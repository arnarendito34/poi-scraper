"""Gabungkan CSV hasil tiap chunk, dedup, tulis ke tab hasil, upload Drive."""
import argparse
import copy
import glob
import json
import os
import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import poi_scraper as s  # noqa: E402
import cloud  # noqa: E402

DEDUP_KEYS = [
    "Brand", "Name_geocode", "Address_geocode",
    "Latitude_geocode", "Longitude_geocode",
]


def entry_targets(spec, profile, admin_df, entry):
    """Hitung daftar target (admin) untuk satu entry plan."""
    if not entry or entry.get("chunk") == "__SKIP__":
        return set()
    start = entry.get("start")
    end = entry.get("end")
    if start is None and end is None:
        targets = s.spec_to_targets(spec, admin_df, profile)
    else:
        chunk_spec = copy.deepcopy(spec)
        top_level = profile["levels"][0]
        chunk_spec.setdefault("scope", {})
        chunk_spec["scope"][top_level] = [entry["prov"]]
        targets = s.spec_to_targets(chunk_spec, admin_df, profile)
        targets = targets[int(start or 0): int(end) if end is not None else None]
    return {t["admin"] for t in targets}


def parse_env_list(name):
    try:
        value = json.loads(os.environ.get(name) or "[]")
    except Exception:  # noqa: BLE001
        return []
    return value if isinstance(value, list) else []


def load_chunks(job_id):
    """Baca chunks_json (status per chunk) untuk menentukan kelengkapan job."""
    try:
        raw = cloud.get_job_field(job_id, "chunks_json") or ""
        data = json.loads(raw) if raw.strip().startswith("{") else {}
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def load_markers(parts_dir):
    """Baca marker Chunkstatus_<idx>.json dari artifact (status final per chunk).

    Marker ditulis run_job setelah semua data di-flush. Ini menghindari race
    read-modify-write pada kolom chunks_json saat beberapa chunk selesai bersamaan.
    """
    markers = {}
    pattern = os.path.join(parts_dir, "**", "Chunkstatus_*.json")
    for path in glob.glob(pattern, recursive=True):
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        except Exception as exc:  # noqa: BLE001
            print(f"WARN gagal baca marker {path}: {exc}")
            continue
        if isinstance(data, dict) and data.get("idx") is not None:
            markers[str(data["idx"])] = data
    return markers


def entry_status(chunks, markers, entry):
    """Status chunk: marker artifact (bila ada) lebih dipercaya daripada chunks_json."""
    if not entry:
        return ""
    idx = str(entry.get("idx"))
    marker = markers.get(idx)
    if isinstance(marker, dict) and marker.get("status"):
        return str(marker.get("status")).strip().lower()
    value = (chunks.get(idx) or {}) if chunks else {}
    return str(value.get("status") or "").strip().lower()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", required=True)
    parser.add_argument("--parts", default="parts")
    parser.add_argument("--workflow-result", default="success")
    parser.add_argument("--plan-result", default="success")
    args = parser.parse_args()

    spec = json.loads(Path(args.spec).read_text(encoding="utf-8-sig"))
    job_id = spec.get("job_id", "")
    user = spec.get("user", "")
    result_sheet = spec.get("result_sheet", "") or f"Result_{job_id}"

    if args.plan_result != "success":
        cloud.update_job(job_id, {
            "status": "failed",
            "error": "Plan gagal — cek log GitHub Actions (job 'plan'). Tidak ada chunk yang dijalankan.",
            "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        cloud.append_log(job_id, user, "merge: plan gagal, tidak ada chunk dijalankan")
        print("MERGE SKIPPED: plan failed", flush=True)
        return

    files = [
        f for f in glob.glob(os.path.join(args.parts, "**", "*.csv"), recursive=True)
        if Path(f).name.lower().startswith("output_")
    ]

    frames = []
    for path in files:
        try:
            frame = pd.read_csv(path, dtype=str, keep_default_na=False)
            if not frame.empty:
                frames.append(frame)
        except Exception as exc:  # noqa: BLE001
            print(f"WARN gagal baca {path}: {exc}")

    # Sertakan data yang sudah ada di tab hasil (penting untuk resume/pause).
    try:
        tab_rows = cloud.fetch_tab_rows(result_sheet)
        if len(tab_rows) > 1:
            header = tab_rows[0]
            body = tab_rows[1:]
            try:
                tab_frame = pd.DataFrame(body, columns=header).reindex(columns=s.OUTPUT_COLUMNS).fillna("")
                if not tab_frame.empty:
                    frames.append(tab_frame)
            except Exception as exc:  # noqa: BLE001
                print(f"WARN tab frame gagal: {exc}")
    except Exception as exc:  # noqa: BLE001
        print(f"WARN fetch tab gagal: {exc}")

    finished = time.strftime("%Y-%m-%d %H:%M:%S")
    chunks = load_chunks(job_id)
    markers = load_markers(args.parts)
    chunks_known = bool(chunks)
    status_source_known = chunks_known or bool(markers)
    if markers:
        print(f"MARKERS {len(markers)} chunk", flush=True)
    try:
        current_status = str(cloud.get_job_field(job_id, "status") or "").strip().lower()
    except Exception:  # noqa: BLE001
        current_status = ""
    try:
        current_overall = float(cloud.get_job_field(job_id, "overall_progress") or 0)
    except Exception:  # noqa: BLE001
        current_overall = 0.0

    resume_done = {
        str(v).strip() for v in (spec.get("resume_done_chunks") or []) if str(v).strip()
    }

    def entry_done(entry):
        """Chunk dianggap selesai bila di-skip saat Resume atau status finalnya done."""
        if not entry:
            return False
        if entry.get("chunk") in resume_done:
            return True
        if entry.get("prov") and entry.get("prov") in resume_done:
            return True
        return entry_status(chunks, markers, entry) == "done"

    full = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=s.OUTPUT_COLUMNS)
    full = full.reindex(columns=s.OUTPUT_COLUMNS).fillna("")
    before = len(full)

    # 1) Dedup berdasarkan Place_ID (bila ada) agar duplikat lintas-chunk pasti hilang.
    if "Place_ID" in full.columns:
        pid = full["Place_ID"].astype(str).str.strip()
        has_pid = pid != ""
        by_pid = full[has_pid].drop_duplicates(subset=["Place_ID"])
        rest = full[~has_pid]
        full = pd.concat([by_pid, rest], ignore_index=True)

    # 2) Dedup fallback berdasarkan brand/nama/alamat/koordinat.
    keys = [c for c in DEDUP_KEYS if c in full.columns]
    full = full.drop_duplicates(subset=keys) if keys else full.drop_duplicates()

    # 3) Audit cakupan target: bandingkan target yang direncanakan vs yang ada di hasil.
    coverage_line = ""
    missing_todo = set()
    incomplete_entries = []
    all_entries = parse_env_list("ALL_ENTRIES")
    todo_entries = parse_env_list("PLAN_ENTRIES") or all_entries
    if status_source_known and todo_entries:
        for entry in todo_entries:
            if not entry or entry.get("chunk") == "__SKIP__":
                continue
            if entry_done(entry):
                continue
            chunk_status = entry_status(chunks, markers, entry)
            incomplete_entries.append(
                (entry.get("chunk") or str(entry.get("idx")), chunk_status or "tidak berjalan")
            )
    # Status chunk adalah sumber kebenaran: chunk "done" berarti semua targetnya sudah
    # diproses, termasuk target yang hasilnya 0 / tersaring (tidak punya baris di tab).
    complete_by_chunks = None
    if status_source_known and todo_entries:
        complete_by_chunks = not incomplete_entries
    if all_entries:
        try:
            profile = s.get_profile(spec.get("country", "indonesia"))
            admin_df = s.load_admin_data(profile)
            expected_all = set()
            processed_all = set()
            for entry in all_entries:
                targets = entry_targets(spec, profile, admin_df, entry)
                expected_all |= targets
                if entry_done(entry):
                    processed_all |= targets
            expected_todo = set()
            processed_todo = set()
            for entry in todo_entries:
                targets = entry_targets(spec, profile, admin_df, entry)
                expected_todo |= targets
                if entry_done(entry):
                    processed_todo |= targets
            present = {
                str(v).strip() for v in full.get("Query_Target", pd.Series(dtype=str))
                if str(v).strip()
            }
            zeros = set()
            try:
                zeros = set(json.loads(cloud.get_job_field(job_id, "zero_targets_json") or "[]") or [])
            except Exception:  # noqa: BLE001
                zeros = set()
            verified = present | {str(z).strip() for z in zeros if str(z).strip()}
            missing_all = expected_all - (verified | processed_all)
            missing_todo = expected_todo - (verified | processed_todo)
            total_expected = len(expected_all)
            covered = total_expected - len(missing_all)
            coverage_line = f"cakupan target: {covered}/{total_expected}"
            if missing_all:
                sample = ", ".join(sorted(missing_all)[:10])
                coverage_line += f" · belum terverifikasi {len(missing_all)} (mis. {sample})"
            cloud.append_log(job_id, user, "audit " + coverage_line)
            print("AUDIT " + coverage_line, flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"WARN audit cakupan gagal: {exc}")

    rows = full.values.tolist()
    try:
        cloud.overwrite_tab(result_sheet, list(s.OUTPUT_COLUMNS), rows)
    except Exception as exc:  # noqa: BLE001
        print(f"WARN overwrite_tab gagal: {exc}")

    out_path = f"Output_final_{job_id or 'result'}.csv"
    full.to_csv(out_path, index=False, encoding="utf-8-sig")

    file_id, link = "", ""
    try:
        file_id, link = cloud.upload_csv(
            out_path, name=out_path, share_email=os.environ.get("SHARE_EMAIL")
        )
    except Exception as exc:  # noqa: BLE001
        # Tidak fatal: Apps Script (akun pemilik) akan mengekspor CSV dari tab hasil
        # via exportCompleted(), karena service account tidak punya kuota Drive.
        print(f"WARN upload Drive gagal (akan diekspor oleh Apps Script): {exc}")

    status = "done" if args.workflow_result == "success" else "failed"
    if complete_by_chunks is True:
        status = "done"
    elif complete_by_chunks is False:
        status = "failed"
    if status == "done" and missing_todo:
        status = "failed"
    if status != "done" and current_status in ("paused", "cancelled"):
        # Jangan timpa status pause/cancel yang diminta pengguna; Resume masih tersedia.
        status = current_status

    progress_text = f"100% ({len(full)} records)"
    if coverage_line:
        progress_text += " · " + coverage_line

    fields = {
        "status": status,
        "finished_at": finished,
        "output_file_id": file_id,
        "output_url": link,
        "result_sheet": result_sheet,
        "records": len(full),
    }
    if status == "done":
        fields["progress"] = progress_text
        fields["overall_progress"] = 100
    else:
        safe_pct = min(max(current_overall, 0.0), 100.0)
        if safe_pct >= 100:
            safe_pct = 99.9
        suffix = {"paused": "dijeda", "cancelled": "dibatalkan"}.get(status, "belum tuntas")
        fields["progress"] = f"{safe_pct:.1f}% ({len(full)} records) · {suffix}"
        fields["overall_progress"] = safe_pct

    if status == "failed":
        # Jangan menimpa pesan error spesifik dari worker bila sudah ada.
        try:
            existing_error = cloud.get_job_field(job_id, "error")
        except Exception:  # noqa: BLE001
            existing_error = ""
        if not existing_error:
            parts = []
            if incomplete_entries:
                shown = ", ".join(f"{label} ({st})" for label, st in incomplete_entries[:5])
                if len(incomplete_entries) > 5:
                    shown += f", +{len(incomplete_entries) - 5} lainnya"
                parts.append(f"{len(incomplete_entries)} chunk belum selesai: {shown}.")
            if missing_todo:
                parts.append(f"{len(missing_todo)} target belum terverifikasi.")
            if coverage_line:
                parts.append(coverage_line + ".")
            if not parts:
                parts.append(
                    "Sebagian chunk gagal / tidak berjalan (workflow-result=" + args.workflow_result +
                    "). Cek log GitHub Actions."
                )
            message = " ".join(parts)
            if missing_todo or incomplete_entries:
                message += " Klik Resume untuk melanjutkan — data tetap tersimpan di tab hasil ini."
            fields["error"] = message

    cloud.update_job(job_id, fields)
    cloud.append_log(job_id, user, f"selesai: {before} -> {len(full)} records (dedup), status={status}")
    print(f"MERGED {before} -> {len(full)} records | {link}", flush=True)


if __name__ == "__main__":
    main()

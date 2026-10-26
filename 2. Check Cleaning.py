import argparse
import json
import os
import re
import sys
import threading
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import pandas as pd

DEFAULT_BLACKLIST_NAMES = [
    "Jaya",
]

DEFAULT_BLACKLIST_CATEGORIES = [
    "Corporate office",
    "Bakery",
    "Bus stop",
    "ATM",
    "Dessert shop",
    "Coffee shop",
    "Tea store",
    "Parking lot",
    "Deli",
    "Vaporizer store",
    "Employment agency",
    "Restaurant",
    "Fast food restaurant",
    "Ice cream shop",
    "Coffee stand",
    "Air compressor supplier",
    "Mine",
    "Vegetable wholesaler",
    "Logistics service",
    "Company",
    "General contractor",
    "Public parking space",
    "Non-governmental organization",
    "Air conditioning store",
    "Construction company",
    "Pharmacy",
    "Vegetarian restaurant",
    "Night club",
    "Tea house",
    "Bubble tea store",
    "Juice shop",
    "Ranch",
    "Electronics store",
    "Office supply store",
    "Beverage distributor",
    "Pest control service",
    "Nasi goreng restaurant",
    "Porridge restaurant",
    "Auto machine shop",
    "Freight forwarding service",
    "Display stand manufacturer",
    "Hawker stall",
    "Distribution service",
    "Electric motorcycle dealer",
    "Health food store",
    "Saw mill",
    "Chocolate cafe",
    "Brunch restaurant",
    "Fish store",
    "Tofu shop",
    "Computer repair service",
    "Stationery store",
    "Feed manufacturer",
    "Cigar shop",
    "Bank",
    "Hawker center",
]

KNOWN_COLUMNS = [
    "Name_geocode",
    "Address_geocode",
    "Longitude_geocode",
    "Latitude_geocode",
    "Category_geocode",
    "Source_URL",
]
COMPACT_COLUMNS = [
    "Name_geocode",
    "Address_geocode",
    "Longitude_geocode",
    "Latitude_geocode",
]
OUTPUT_NAME_RE = re.compile(
    r"_(?:clean|removed|pending|review)(?:_\d+)?\.csv$", re.IGNORECASE
)
VALID_DECISIONS = ("pending", "keep", "remove")
FILTERS = ("all", "pending", "risky", "keep", "remove")
RISK_LABELS = {
    "prefix": "nama tidak diawali brand",
    "nama": "blacklist nama",
    "kategori": "kategori non-retail",
    "kosong": "nama kosong",
}


def configure_stdout():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass


def now_text():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def expand_path(value):
    text = str(value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1]
    text = os.path.expandvars(os.path.expanduser(text))
    return Path(text)


def format_size(size):
    size = float(size or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} GB"


def is_candidate_csv(path):
    if OUTPUT_NAME_RE.search(path.name):
        return False
    return path.suffix.lower() == ".csv"


def list_csv_files(folder):
    try:
        files = [p for p in Path(folder).glob("*.csv") if p.is_file() and is_candidate_csv(p)]
    except OSError:
        return []
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files


def prompt_text(label, default=None):
    suffix = f" [{default}]" if default not in (None, "") else ""
    try:
        value = input(f"{label}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nDibatalkan.")
        raise SystemExit(130)
    if value:
        return value
    return default if default is not None else ""


def resolve_csv_path(initial=None):
    if initial:
        path = expand_path(initial)
        if path.is_file():
            return path.resolve()
        raise FileNotFoundError(f"File CSV tidak ditemukan: {path}")

    folder = Path.cwd()
    while True:
        files = list_csv_files(folder)
        print("\nFile CSV di folder ini:")
        if files:
            for index, path in enumerate(files[:15], 1):
                stat = path.stat()
                print(f"  {index}. {path.name}  ({format_size(stat.st_size)})")
            if len(files) > 15:
                print(f"  ... dan {len(files) - 15} file lainnya")
        else:
            print("  (tidak ada file .csv)")
        print("  0. Tempel path manual (bisa drag & drop file ke sini)")

        raw = prompt_text("Pilih nomor atau path CSV")
        if not raw:
            print("Path tidak boleh kosong.")
            continue

        if raw.isdigit():
            choice = int(raw)
            if choice == 0:
                raw = prompt_text("Path CSV")
                if not raw:
                    continue
                path = expand_path(raw)
            elif 1 <= choice <= min(len(files), 15):
                path = files[choice - 1]
            else:
                print("Nomor tidak tersedia.")
                continue
        else:
            path = expand_path(raw)

        if path.is_dir():
            folder = path.resolve()
            continue
        if not path.is_file():
            print(f"File tidak ditemukan: {path}")
            continue
        return path.resolve()


def detect_encoding(path, forced=None):
    if forced:
        return forced
    last_error = None
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            with open(path, "r", encoding=encoding, newline="") as handle:
                handle.read(65536)
            return encoding
        except UnicodeDecodeError as exc:
            last_error = exc
    if last_error:
        raise last_error
    return "utf-8"


def detect_delimiter(path, encoding, forced=None):
    if forced:
        return forced
    try:
        with open(path, "r", encoding=encoding, newline="") as handle:
            sample = handle.read(65536)
    except OSError:
        return ","
    first_line = ""
    for line in sample.splitlines():
        if line.strip():
            first_line = line
            break
    counts = {delimiter: first_line.count(delimiter) for delimiter in (",", ";", "\t", "|")}
    best = max(counts, key=lambda delimiter: counts[delimiter])
    return best if counts[best] > 0 else ","


def read_csv_input(path, encoding=None, delimiter=None):
    chosen_encoding = detect_encoding(path, encoding)
    chosen_delimiter = detect_delimiter(path, chosen_encoding, delimiter)
    try:
        frame = pd.read_csv(
            path,
            encoding=chosen_encoding,
            sep=chosen_delimiter,
            dtype=str,
            keep_default_na=False,
            skip_blank_lines=True,
        )
    except Exception as exc:
        raise ValueError(f"CSV tidak bisa dibaca: {exc}") from exc
    if len(frame):
        non_empty = pd.Series(False, index=frame.index)
        for column in frame.columns:
            non_empty |= frame[column].astype(str).str.strip().ne("")
        frame = frame.loc[non_empty].reset_index(drop=True)
    return frame, chosen_encoding, chosen_delimiter


def find_column(frame, canonical):
    if canonical in frame.columns:
        return canonical
    wanted = canonical.strip().lower()
    for column in frame.columns:
        if str(column).strip().lstrip("\ufeff").lower() == wanted:
            return column
    return None


def normalize_columns(frame):
    renames = {}
    for canonical in KNOWN_COLUMNS:
        if canonical in frame.columns:
            continue
        found = find_column(frame, canonical)
        if found is not None and found not in renames:
            renames[found] = canonical
    if renames:
        frame = frame.rename(columns=renames)
    return frame


def split_terms(value):
    terms = []
    seen = set()
    for item in re.split(r"[;,|]", str(value or "")):
        term = item.strip().strip("\"'").strip()
        key = term.casefold()
        if term and key not in seen:
            seen.add(key)
            terms.append(term)
    return terms


def cell_text(value):
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
    return str(value).replace("\x00", "").strip()


def compile_brand_patterns(brand_terms):
    patterns = []
    for term in brand_terms:
        source = "^" + re.escape(term).replace(r"\ ", r"[\s-]*") + r"\b"
        patterns.append(re.compile(source, re.IGNORECASE))
    return patterns


def assess_row(name, category, brand_terms, brand_patterns, blacklist_names, blacklist_categories):
    risks = []
    reasons = []
    name = cell_text(name)
    category = cell_text(category)

    if not name:
        risks.append("kosong")
        reasons.append("nama kosong")
    elif brand_patterns and not any(pattern.match(name) for pattern in brand_patterns):
        label = " / ".join(brand_terms)
        risks.append("prefix")
        reasons.append(f'nama tidak diawali "{label}"')

    name_hits = [
        term for term in blacklist_names if term.casefold() in name.casefold()
    ]
    if name_hits:
        risks.append("nama")
        reasons.append("nama memuat " + ", ".join(f'"{term}"' for term in name_hits))

    category_hits = [
        term for term in blacklist_categories if term.casefold() in category.casefold()
    ]
    if category_hits:
        risks.append("kategori")
        reasons.append("kategori " + ", ".join(f'"{term}"' for term in category_hits))

    return risks, "; ".join(reasons)


def valid_http_url(value):
    text = cell_text(value)
    if not text:
        return ""
    parsed = urlparse(text)
    if parsed.scheme in ("http", "https") and parsed.netloc:
        return text
    return ""


def maps_url_for(name, address, latitude, longitude, source_url):
    source = valid_http_url(source_url)
    if source:
        return source
    query = " ".join(part for part in (cell_text(name), cell_text(address)) if part).strip()
    if not query:
        coordinates = ",".join(
            part for part in (cell_text(latitude), cell_text(longitude)) if part
        )
        query = coordinates
    if not query:
        return ""
    return "https://www.google.com/maps/search/?api=1&query=" + quote(query)


def infer_brand(frame):
    for column in frame.columns:
        if str(column).strip().lower() == "brand":
            values = []
            for value in frame[column]:
                value = cell_text(value)
                if value and value not in values:
                    values.append(value)
            if len(values) == 1:
                return values[0]
    return ""


def unique_path(path):
    if not path.exists():
        return path
    for index in range(2, 1000):
        candidate = path.with_name(f"{path.stem}_{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise ValueError(f"Tidak bisa membuat nama file unik untuk: {path}")


def build_signature(path, frame, brand, blacklist_names, blacklist_categories, review_all):
    try:
        stat = path.stat()
        size = int(stat.st_size)
        mtime_ns = int(stat.st_mtime_ns)
    except OSError:
        size = 0
        mtime_ns = 0
    return {
        "size": size,
        "mtime_ns": mtime_ns,
        "rows": int(len(frame)),
        "columns": [str(column) for column in frame.columns],
        "brand": brand,
        "blacklist_names": list(blacklist_names),
        "blacklist_categories": list(blacklist_categories),
        "review_all": bool(review_all),
    }


class ReviewSession:
    def __init__(
        self,
        frame,
        input_path,
        brand,
        brand_terms,
        brand_patterns,
        blacklist_names,
        blacklist_categories,
        review_all,
        progress_path,
        output_dir,
        compact_default=False,
        force_outputs=False,
        reset_progress=False,
    ):
        self.frame = frame
        self.input_path = input_path
        self.brand = brand
        self.brand_terms = brand_terms
        self.brand_patterns = brand_patterns
        self.blacklist_names = blacklist_names
        self.blacklist_categories = blacklist_categories
        self.review_all = review_all
        self.progress_path = progress_path
        self.output_dir = output_dir
        self.compact_default = compact_default
        self.force_outputs = force_outputs
        self.reset_progress = reset_progress
        self.lock = threading.RLock()
        self.history = []
        self.finalized = False
        self.outputs = {}
        self.resumed = False
        self.progress_error = ""
        self.signature = build_signature(
            input_path, frame, brand, blacklist_names, blacklist_categories, review_all
        )

        self.rows = []
        self.decisions = []
        for index in range(len(frame)):
            record = frame.iloc[index]
            values = {}
            for column in KNOWN_COLUMNS:
                if column in frame.columns:
                    values[column] = cell_text(record.get(column))
                else:
                    values[column] = ""
            risks, reason = assess_row(
                values["Name_geocode"],
                values["Category_geocode"],
                brand_terms,
                brand_patterns,
                blacklist_names,
                blacklist_categories,
            )
            decision = "pending" if (review_all or risks) else "keep"
            self.rows.append(
                {
                    "row_id": index + 1,
                    "name": values["Name_geocode"],
                    "address": values["Address_geocode"],
                    "longitude": values["Longitude_geocode"],
                    "latitude": values["Latitude_geocode"],
                    "category": values["Category_geocode"],
                    "source_url": values["Source_URL"],
                    "maps_url": maps_url_for(
                        values["Name_geocode"],
                        values["Address_geocode"],
                        values["Latitude_geocode"],
                        values["Longitude_geocode"],
                        values["Source_URL"],
                    ),
                    "risk": risks,
                    "reason": reason,
                }
            )
            self.decisions.append(decision)

        self.load_progress()

    def load_progress(self):
        if self.reset_progress or not self.progress_path:
            return
        if not self.progress_path.exists():
            return
        try:
            data = json.loads(self.progress_path.read_text(encoding="utf-8"))
        except Exception as exc:
            self.backup_bad_progress(f"progress tidak bisa dibaca ({exc})")
            return
        if data.get("version") != 1 or data.get("signature") != self.signature:
            self.backup_bad_progress("file/opsi input berubah")
            return
        decisions = data.get("decisions")
        if (
            not isinstance(decisions, list)
            or len(decisions) != len(self.decisions)
            or any(value not in VALID_DECISIONS for value in decisions)
        ):
            self.backup_bad_progress("format keputusan tidak cocok")
            return

        self.decisions = decisions
        self.resumed = True
        if data.get("finalized"):
            outputs = data.get("outputs")
            if isinstance(outputs, dict):
                existing = {}
                for key, info in outputs.items():
                    if not isinstance(info, dict):
                        continue
                    path = Path(str(info.get("path") or ""))
                    if path.is_file():
                        existing[key] = {"path": str(path), "name": path.name}
                if {"clean", "removed", "review"} <= set(existing):
                    self.outputs = existing
                    self.finalized = True
        print(f"Progress dimuat: {self.progress_path}")

    def backup_bad_progress(self, reason):
        if not self.progress_path or not self.progress_path.exists():
            return
        self.progress_error = f"Progress lama diabaikan: {reason}"
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = self.progress_path.with_name(f"{self.progress_path.name}.bak-{stamp}")
        try:
            os.replace(self.progress_path, backup)
            print(f"Progress lama diabaikan ({reason}); dipindahkan ke {backup}")
        except OSError as exc:
            print(f"Progress lama diabaikan ({reason}); gagal membuat backup: {exc}")

    def save_progress(self):
        if not self.progress_path:
            return
        payload = {
            "version": 1,
            "input": str(self.input_path),
            "signature": self.signature,
            "updated_at": now_text(),
            "brand": self.brand,
            "review_all": self.review_all,
            "decisions": list(self.decisions),
            "finalized": self.finalized,
            "outputs": self.outputs,
        }
        try:
            self.progress_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.progress_path.with_name(self.progress_path.name + ".tmp")
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=1)
            os.replace(temporary, self.progress_path)
            self.progress_error = ""
        except Exception as exc:
            message = f"Progress gagal disimpan: {exc}"
            if self.progress_error != message:
                print(message, file=sys.stderr)
                self.progress_error = message

    def summary(self):
        counts = {
            "all": len(self.rows),
            "pending": 0,
            "risky": 0,
            "keep": 0,
            "remove": 0,
        }
        for row, decision in zip(self.rows, self.decisions):
            if decision == "pending":
                counts["pending"] += 1
            elif decision == "keep":
                counts["keep"] += 1
            elif decision == "remove":
                counts["remove"] += 1
            if row["risk"]:
                counts["risky"] += 1
        stats = {
            "total": counts["all"],
            "kept": counts["keep"],
            "removed": counts["remove"],
            "pending": counts["pending"],
            "risky": counts["risky"],
            "safe": counts["all"] - counts["risky"],
        }
        return stats, counts

    def filtered_indices(self, filter_key, query):
        if filter_key not in FILTERS:
            filter_key = "all"
        query = str(query or "").strip().casefold()
        indices = []
        for index, row in enumerate(self.rows):
            decision = self.decisions[index]
            if filter_key == "pending" and decision != "pending":
                continue
            if filter_key == "keep" and decision != "keep":
                continue
            if filter_key == "remove" and decision != "remove":
                continue
            if filter_key == "risky" and not row["risk"]:
                continue
            if query:
                haystack = (
                    row["name"] + " " + row["address"] + " " + row["category"]
                ).casefold()
                if query not in haystack:
                    continue
            indices.append(index)
        return indices

    def row_payload(self, index):
        row = dict(self.rows[index])
        row["decision"] = self.decisions[index]
        row["risk_label"] = ", ".join(
            RISK_LABELS.get(code, code) for code in row["risk"]
        )
        return row

    def result_payload(self, indices, pos):
        stats, counts = self.summary()
        if indices:
            pos = max(0, min(int(pos), len(indices) - 1))
            row = self.row_payload(indices[pos])
        else:
            pos = 0
            row = None
        return {
            "row": row,
            "pos": pos,
            "filtered": len(indices),
            "stats": stats,
            "counts": counts,
            "can_undo": bool(self.history),
            "saved": not bool(self.progress_error),
            "progress_error": self.progress_error,
        }

    def current(self, filter_key, query, pos):
        with self.lock:
            indices = self.filtered_indices(filter_key, query)
            try:
                pos = int(pos)
            except (TypeError, ValueError):
                pos = 0
            payload = self.result_payload(indices, pos)
            payload["filter"] = filter_key
            payload["q"] = query
            return payload

    def decide(self, row_id, decision, filter_key, query, pos):
        with self.lock:
            if self.finalized:
                raise ValueError("Review sudah difinalisasi; keputusan tidak bisa diubah.")
            decision = str(decision or "").lower()
            if decision not in VALID_DECISIONS:
                raise ValueError("Keputusan tidak dikenal.")
            try:
                index = int(row_id) - 1
            except (TypeError, ValueError):
                raise ValueError("Baris tidak valid.")
            if index < 0 or index >= len(self.rows):
                raise ValueError("Baris tidak valid.")

            before = self.filtered_indices(filter_key, query)
            try:
                pos_before = before.index(index)
            except ValueError:
                try:
                    pos_before = int(pos)
                except (TypeError, ValueError):
                    pos_before = 0

            previous = self.decisions[index]
            changed = previous != decision
            if changed:
                self.history.append((index, previous))
                if len(self.history) > 1000:
                    self.history = self.history[-1000:]
                self.decisions[index] = decision
                self.save_progress()

            after = self.filtered_indices(filter_key, query)
            if index in after:
                new_pos = after.index(index) + 1
                if new_pos >= len(after):
                    new_pos = len(after) - 1
            else:
                new_pos = pos_before
            payload = self.result_payload(after, new_pos)
            payload["filter"] = filter_key
            payload["q"] = query
            payload["changed"] = changed
            payload["previous"] = previous
            return payload

    def undo(self, filter_key, query, pos):
        with self.lock:
            if self.finalized:
                raise ValueError("Review sudah difinalisasi; keputusan tidak bisa diubah.")
            if not self.history:
                raise ValueError("Belum ada keputusan yang bisa diurungkan.")
            index, previous = self.history.pop()
            self.decisions[index] = previous
            self.save_progress()

            after = self.filtered_indices(filter_key, query)
            if index in after:
                new_pos = after.index(index)
            else:
                try:
                    new_pos = int(pos)
                except (TypeError, ValueError):
                    new_pos = 0
            payload = self.result_payload(after, new_pos)
            payload["filter"] = filter_key
            payload["q"] = query
            return payload

    def public_outputs(self):
        result = {}
        for key, info in self.outputs.items():
            if not isinstance(info, dict):
                continue
            path = Path(str(info.get("path") or ""))
            result[key] = {
                "path": str(path),
                "name": path.name or key + ".csv",
                "url": f"/download/{key}",
            }
        return result

    def state_payload(self):
        with self.lock:
            stats, counts = self.summary()
            return {
                "meta": {
                    "input": self.input_path.name,
                    "input_path": str(self.input_path),
                    "brand": self.brand,
                    "brand_terms": list(self.brand_terms),
                    "review_all": bool(self.review_all),
                    "total": len(self.rows),
                    "finalized": bool(self.finalized),
                    "resumed": bool(self.resumed),
                    "progress_path": str(self.progress_path or ""),
                    "progress_error": self.progress_error,
                    "blacklist_names": list(self.blacklist_names),
                    "blacklist_categories_count": len(self.blacklist_categories),
                    "compact_default": bool(self.compact_default),
                    "can_undo": bool(self.history),
                },
                "stats": stats,
                "counts": counts,
                "outputs": self.public_outputs(),
                "default_filter": "pending" if counts["pending"] else "all",
            }

    def finalize(self, compact=False, allow_pending=False):
        with self.lock:
            if self.finalized:
                stats, counts = self.summary()
                return {
                    "kept": counts["keep"],
                    "removed": counts["remove"],
                    "pending": counts["pending"],
                    "outputs": self.public_outputs(),
                    "stats": stats,
                    "counts": counts,
                }

            stats, counts = self.summary()
            if counts["pending"] and not allow_pending:
                raise ValueError(
                    f"Masih ada {counts['pending']} baris belum diputuskan. "
                    "Konfirmasi agar baris tersebut masuk file pending."
                )

            keep_indices = [
                index for index, decision in enumerate(self.decisions) if decision == "keep"
            ]
            remove_indices = [
                index for index, decision in enumerate(self.decisions) if decision == "remove"
            ]
            pending_indices = [
                index for index, decision in enumerate(self.decisions) if decision == "pending"
            ]

            keep_frame = self.frame.iloc[keep_indices].copy()
            if compact:
                missing = [
                    column for column in COMPACT_COLUMNS if column not in keep_frame.columns
                ]
                if missing:
                    raise ValueError(
                        "Format ringkas membutuhkan kolom: " + ", ".join(missing)
                    )
                keep_frame = keep_frame[COMPACT_COLUMNS]

            removed_frame = self.frame.iloc[remove_indices].copy()
            removed_frame["Reason"] = [
                self.rows[index]["reason"] or "manual"
                for index in remove_indices
            ]

            pending_frame = self.frame.iloc[pending_indices].copy()

            audit_frame = self.frame.copy()
            for column in ("_decision", "_risk", "_reason"):
                if column in audit_frame.columns:
                    audit_frame = audit_frame.drop(columns=[column])
            audit_frame.insert(0, "_decision", list(self.decisions))
            audit_frame.insert(
                1, "_risk", [",".join(row["risk"]) for row in self.rows]
            )
            audit_frame.insert(2, "_reason", [row["reason"] for row in self.rows])

            self.output_dir.mkdir(parents=True, exist_ok=True)
            base = self.input_path.stem

            def target(suffix):
                path = self.output_dir / f"{base}_{suffix}.csv"
                return path if self.force_outputs else unique_path(path)

            clean_path = target("clean")
            removed_path = target("removed")
            review_path = target("review")

            keep_frame.to_csv(clean_path, index=False, encoding="utf-8-sig")
            removed_frame.to_csv(removed_path, index=False, encoding="utf-8-sig")
            review_path.parent.mkdir(parents=True, exist_ok=True)
            audit_frame.to_csv(review_path, index=False, encoding="utf-8-sig")

            outputs = {
                "clean": {"path": str(clean_path), "name": clean_path.name},
                "removed": {"path": str(removed_path), "name": removed_path.name},
                "review": {"path": str(review_path), "name": review_path.name},
            }
            if pending_indices:
                pending_path = target("pending")
                pending_frame.to_csv(pending_path, index=False, encoding="utf-8-sig")
                outputs["pending"] = {
                    "path": str(pending_path),
                    "name": pending_path.name,
                }

            self.outputs = outputs
            self.finalized = True
            self.save_progress()
            return {
                "kept": counts["keep"],
                "removed": counts["remove"],
                "pending": counts["pending"],
                "outputs": self.public_outputs(),
                "stats": stats,
                "counts": counts,
                "saved": not bool(self.progress_error),
                "progress_error": self.progress_error,
            }


def build_parser():
    parser = argparse.ArgumentParser(
        description="Review manual cleaning data lewat browser lokal.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Contoh:\n"
            '  python "2. Check Cleaning.py"\n'
            '  python "2. Check Cleaning.py" -i hasil.csv -b Alfamart\n'
            '  python "2. Check Cleaning.py" -i hasil.csv -b "Alfamart|Alfamidi" --review all\n'
            '  python "2. Check Cleaning.py" -i hasil.csv -b Alfamart --port 9000 --no-browser'
        ),
    )
    parser.add_argument("input", nargs="?", help="Path file CSV yang akan direview.")
    parser.add_argument(
        "-i",
        "--input",
        dest="input_opt",
        metavar="INPUT",
        help="Path file CSV yang akan direview.",
    )
    parser.add_argument(
        "-b",
        "--brand",
        help="Nama kompetitor/brand. Kosongkan untuk melewati cek prefix.",
    )
    parser.add_argument(
        "--review",
        choices=("risky", "all"),
        default="risky",
        help="risky = baris aman otomatis disimpan; all = semua baris wajib dicek.",
    )
    parser.add_argument("--blacklist-names", help="Override blacklist nama (pisahkan koma).")
    parser.add_argument(
        "--blacklist-categories", help="Override blacklist kategori (pisahkan koma)."
    )
    parser.add_argument("--host", default="127.0.0.1", help="Host server (default 127.0.0.1).")
    parser.add_argument("--port", type=int, default=8765, help="Port server (0 = acak).")
    parser.add_argument("--no-browser", action="store_true", help="Jangan buka browser otomatis.")
    parser.add_argument("--output-dir", help="Folder untuk file hasil review.")
    parser.add_argument("--progress", help="Path file progress JSON.")
    parser.add_argument("--encoding", help="Encoding CSV (default: deteksi otomatis).")
    parser.add_argument("--delimiter", help="Delimiter CSV (default: deteksi otomatis).")
    parser.add_argument("--compact", action="store_true", help="Default hasil bersih 4 kolom.")
    parser.add_argument("--force", action="store_true", help="Timpa file hasil yang sudah ada.")
    parser.add_argument("--reset", action="store_true", help="Abaikan progress lama.")
    return parser


def validate_input_columns(frame):
    if "Name_geocode" not in frame.columns:
        available = ", ".join(str(column) for column in frame.columns[:20])
        raise ValueError(
            "Kolom Name_geocode tidak ditemukan. Kolom tersedia: " + available
        )


def run(args):
    print("===================================")
    print("CHECK CLEANING - REVIEW MANUAL")
    print("===================================")
    input_value = args.input_opt or args.input
    if not input_value and not sys.stdin.isatty():
        raise ValueError("Path CSV wajib diisi lewat argumen.")
    input_path = resolve_csv_path(input_value)
    frame, encoding, delimiter = read_csv_input(input_path, args.encoding, args.delimiter)
    frame = normalize_columns(frame)
    validate_input_columns(frame)
    if not len(frame):
        raise ValueError("CSV tidak memiliki baris data.")

    print(f"Input          : {input_path}")
    print(f"Baris          : {len(frame)}")
    print(f"Encoding       : {encoding}")
    print(f"Delimiter      : {repr(delimiter)}")

    brand_default = infer_brand(frame)
    if args.brand is not None:
        brand = args.brand.strip()
    elif sys.stdin.isatty():
        print("\nKetik - untuk melewati cek prefix (semua baris wajib dicek).")
        brand = prompt_text(
            "Nama kompetitor/brand (ketik - untuk kosong)", brand_default
        ).strip()
        if brand == "-":
            brand = ""
    else:
        brand = brand_default

    brand_terms = split_terms(brand)
    brand_patterns = compile_brand_patterns(brand_terms)
    review_all = args.review == "all" or not brand_terms

    blacklist_names = (
        split_terms(args.blacklist_names)
        if args.blacklist_names is not None
        else list(DEFAULT_BLACKLIST_NAMES)
    )
    blacklist_categories = (
        split_terms(args.blacklist_categories)
        if args.blacklist_categories is not None
        else list(DEFAULT_BLACKLIST_CATEGORIES)
    )

    if args.progress:
        progress_path = expand_path(args.progress)
        if not progress_path.is_absolute():
            progress_path = (Path.cwd() / progress_path).resolve()
    else:
        progress_path = input_path.with_name(input_path.name + ".review.json")

    output_dir = (
        expand_path(args.output_dir)
        if args.output_dir
        else input_path.parent
    )
    if not output_dir.is_absolute():
        output_dir = (Path.cwd() / output_dir).resolve()

    session = ReviewSession(
        frame=frame,
        input_path=input_path,
        brand=brand,
        brand_terms=brand_terms,
        brand_patterns=brand_patterns,
        blacklist_names=blacklist_names,
        blacklist_categories=blacklist_categories,
        review_all=review_all,
        progress_path=progress_path,
        output_dir=output_dir,
        compact_default=args.compact,
        force_outputs=args.force,
        reset_progress=args.reset,
    )

    stats = session.summary()[0]
    print(f"Brand          : {brand or '(tanpa brand)'}")
    print(
        "Mode           : "
        + ("semua baris wajib dicek" if review_all else "baris aman otomatis disimpan")
    )
    print(
        "Aturan         : "
        + f"{len(brand_terms)} prefix brand, {len(blacklist_names)} blacklist nama, "
        + f"{len(blacklist_categories)} blacklist kategori"
    )
    print(
        f"Status awal    : {stats['kept']} disimpan, {stats['pending']} perlu dicek, "
        + f"{stats['risky']} berisiko"
    )
    print(f"Progress       : {progress_path}")
    print(f"Output         : {output_dir}")

    start_server(session, args)
    return 0


HTML = r"""<!doctype html>
<html lang="id">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Check Cleaning - Review Manual</title>
<script>
try {
  var savedTheme = localStorage.getItem("cc-theme");
  if (savedTheme) document.documentElement.setAttribute("data-theme", savedTheme);
} catch (err) {}
</script>
<style>
  :root {
    --bg: #f2f6fb;
    --bg2: #e8f0f8;
    --panel: #ffffff;
    --panel2: #f7fafd;
    --text: #0f172a;
    --muted: #64748b;
    --line: #d9e4f0;
    --accent: #0e7490;
    --accent2: #0891b2;
    --accent-soft: #ecfeff;
    --accent-line: #a5f3fc;
    --ok: #047857;
    --ok-soft: #ecfdf5;
    --ok-line: #a7f3d0;
    --warn: #b45309;
    --warn-soft: #fffbeb;
    --warn-line: #fde68a;
    --bad: #be123c;
    --bad-soft: #fff1f2;
    --bad-line: #fecdd3;
    --shadow: 0 18px 50px rgba(15, 23, 42, 0.10);
    --radius: 18px;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --bg: #07101d;
      --bg2: #0b1a2b;
      --panel: #0f1d30;
      --panel2: #12243a;
      --text: #e6eef8;
      --muted: #8ea3bb;
      --line: #213a56;
      --accent: #22d3ee;
      --accent2: #06b6d4;
      --accent-soft: rgba(34, 211, 238, 0.10);
      --accent-line: rgba(34, 211, 238, 0.35);
      --ok: #34d399;
      --ok-soft: rgba(52, 211, 153, 0.10);
      --ok-line: rgba(52, 211, 153, 0.35);
      --warn: #fbbf24;
      --warn-soft: rgba(251, 191, 36, 0.10);
      --warn-line: rgba(251, 191, 36, 0.35);
      --bad: #fb7185;
      --bad-soft: rgba(251, 113, 133, 0.10);
      --bad-line: rgba(251, 113, 133, 0.35);
      --shadow: 0 20px 60px rgba(0, 0, 0, 0.35);
    }
  }
  :root[data-theme="dark"] {
    --bg: #07101d;
    --bg2: #0b1a2b;
    --panel: #0f1d30;
    --panel2: #12243a;
    --text: #e6eef8;
    --muted: #8ea3bb;
    --line: #213a56;
    --accent: #22d3ee;
    --accent2: #06b6d4;
    --accent-soft: rgba(34, 211, 238, 0.10);
    --accent-line: rgba(34, 211, 238, 0.35);
    --ok: #34d399;
    --ok-soft: rgba(52, 211, 153, 0.10);
    --ok-line: rgba(52, 211, 153, 0.35);
    --warn: #fbbf24;
    --warn-soft: rgba(251, 191, 36, 0.10);
    --warn-line: rgba(251, 191, 36, 0.35);
    --bad: #fb7185;
    --bad-soft: rgba(251, 113, 133, 0.10);
    --bad-line: rgba(251, 113, 133, 0.35);
    --shadow: 0 20px 60px rgba(0, 0, 0, 0.35);
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; }
  body {
    min-height: 100vh;
    background:
      radial-gradient(circle at 8% 0%, var(--accent-soft), transparent 32rem),
      radial-gradient(circle at 92% 8%, var(--ok-soft), transparent 28rem),
      linear-gradient(180deg, var(--bg), var(--bg2));
    color: var(--text);
    font-family: "Segoe UI", system-ui, -apple-system, BlinkMacSystemFont, sans-serif;
    font-size: 15px;
    line-height: 1.5;
  }
  button, input { font: inherit; }
  button { cursor: pointer; }
  .shell { width: min(1080px, 100%); margin: 0 auto; padding: 22px 18px 0; }
  .top { display: flex; align-items: center; gap: 14px; margin-bottom: 16px; }
  .brand { display: flex; align-items: center; gap: 12px; min-width: 0; }
  .logo {
    width: 46px; height: 46px; border-radius: 14px; flex: 0 0 auto;
    display: grid; place-items: center; color: #fff;
    background: linear-gradient(135deg, var(--accent2), var(--accent));
    box-shadow: 0 8px 22px rgba(8, 145, 178, 0.30);
  }
  .logo svg { width: 26px; height: 26px; }
  h1 { margin: 0; font-size: 20px; letter-spacing: -0.02em; }
  .subtitle { margin: 1px 0 0; color: var(--muted); font-size: 12px; }
  .top-actions { margin-left: auto; display: flex; gap: 8px; }
  .icon-btn {
    width: 38px; height: 38px; border-radius: 11px; border: 1px solid var(--line);
    background: var(--panel); color: var(--text); display: grid; place-items: center;
    font-weight: 700; transition: 0.15s ease;
  }
  .icon-btn:hover { border-color: var(--accent); color: var(--accent); transform: translateY(-1px); }
  .icon-btn.danger:hover { border-color: var(--bad); color: var(--bad); }
  .session-bar {
    display: flex; flex-wrap: wrap; gap: 8px; align-items: center;
    padding: 12px 14px; border: 1px solid var(--line); border-radius: 16px;
    background: var(--panel); margin-bottom: 14px; box-shadow: var(--shadow);
  }
  .session-bar .item { font-size: 12px; color: var(--muted); }
  .session-bar .item b { color: var(--text); font-weight: 650; }
  .dot { width: 5px; height: 5px; border-radius: 50%; background: var(--line); }
  .stats { display: grid; grid-template-columns: repeat(5, 1fr); gap: 10px; margin-bottom: 12px; }
  .stat {
    padding: 12px 14px; border-radius: 15px; border: 1px solid var(--line);
    background: var(--panel); box-shadow: var(--shadow);
  }
  .stat .label { color: var(--muted); font-size: 10px; letter-spacing: 0.08em; text-transform: uppercase; }
  .stat .value { font-size: 23px; font-weight: 800; letter-spacing: -0.03em; margin-top: 2px; }
  .stat.ok .value { color: var(--ok); }
  .stat.bad .value { color: var(--bad); }
  .stat.warn .value { color: var(--warn); }
  .progress-wrap { margin-bottom: 16px; }
  .progress-track { height: 7px; border-radius: 99px; background: var(--line); overflow: hidden; }
  .progress-bar { height: 100%; width: 0; border-radius: 99px; background: linear-gradient(90deg, var(--accent2), var(--ok)); transition: width 0.25s ease; }
  .progress-text { display: flex; justify-content: space-between; color: var(--muted); font-size: 11px; margin-top: 5px; }
  .toolbar { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; margin-bottom: 12px; }
  .filters { display: flex; flex-wrap: wrap; gap: 6px; }
  .filter-btn {
    border: 1px solid var(--line); background: var(--panel); color: var(--muted);
    border-radius: 999px; padding: 7px 12px; font-size: 12px; font-weight: 600;
    display: inline-flex; align-items: center; gap: 6px; transition: 0.15s ease;
  }
  .filter-btn:hover { border-color: var(--accent); color: var(--accent); }
  .filter-btn.active { border-color: transparent; background: var(--accent); color: #fff; }
  .filter-btn .count { opacity: 0.75; font-weight: 700; }
  .search-wrap { position: relative; margin-left: auto; min-width: 230px; flex: 0 1 320px; }
  .search-wrap input {
    width: 100%; border: 1px solid var(--line); border-radius: 12px;
    background: var(--panel); color: var(--text); padding: 9px 34px 9px 12px;
    outline: none; font-size: 13px;
  }
  .search-wrap input:focus { border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft); }
  .search-wrap input::-webkit-search-cancel-button { -webkit-appearance: none; appearance: none; }
  .search-clear {
    position: absolute; right: 5px; top: 50%; transform: translateY(-50%);
    border: 0; background: transparent; color: var(--muted); padding: 4px 7px;
    border-radius: 8px; font-size: 14px;
  }
  .search-clear:hover { background: var(--panel2); color: var(--text); }
  .card-host { min-height: 340px; }
  .card {
    border: 2px solid var(--line); border-radius: var(--radius);
    background: var(--panel); box-shadow: var(--shadow); padding: 20px;
    animation: rise 0.18s ease;
  }
  @keyframes rise { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: none; } }
  .card.safe { border-color: var(--ok-line); }
  .card.risk { border-color: var(--warn-line); }
  .card.remove { border-color: var(--bad-line); }
  .card-top { display: flex; flex-wrap: wrap; gap: 7px; align-items: center; margin-bottom: 14px; }
  .chip {
    display: inline-flex; align-items: center; gap: 5px;
    border: 1px solid var(--line); border-radius: 999px;
    background: var(--panel2); color: var(--muted);
    padding: 4px 9px; font-size: 11px; font-weight: 600;
  }
  .chip.ok { border-color: transparent; background: var(--ok-soft); color: var(--ok); }
  .chip.warn { border-color: transparent; background: var(--warn-soft); color: var(--warn); }
  .chip.bad { border-color: transparent; background: var(--bad-soft); color: var(--bad); }
  .chip.accent { border-color: transparent; background: var(--accent-soft); color: var(--accent); }
  .card-title { font-size: clamp(21px, 3.6vw, 31px); line-height: 1.18; font-weight: 800; letter-spacing: -0.025em; word-break: break-word; }
  .card-category { color: var(--accent); font-size: 13px; font-weight: 650; margin-top: 5px; word-break: break-word; }
  .meta-grid { display: grid; grid-template-columns: 1.5fr 1fr; gap: 12px; margin-top: 16px; }
  .meta-box { border: 1px solid var(--line); border-radius: 13px; padding: 11px 13px; background: var(--panel2); }
  .meta-box .k { color: var(--muted); font-size: 10px; text-transform: uppercase; letter-spacing: 0.07em; }
  .meta-box .v { margin-top: 3px; font-size: 13px; word-break: break-word; }
  .risk-box { border-radius: 13px; padding: 11px 13px; font-size: 12px; margin-top: 13px; border: 1px solid; }
  .risk-box.warn { background: var(--warn-soft); border-color: var(--warn-line); color: var(--warn); }
  .risk-box.ok { background: var(--ok-soft); border-color: var(--ok-line); color: var(--ok); }
  .link-row { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 13px; }
  .link-btn {
    display: inline-flex; align-items: center; gap: 7px; text-decoration: none;
    border: 1px solid var(--line); border-radius: 10px; padding: 7px 11px;
    background: var(--panel2); color: var(--text); font-size: 12px; font-weight: 600;
  }
  .link-btn:hover { border-color: var(--accent); color: var(--accent); }
  .actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 18px; align-items: center; }
  .btn {
    border: 1px solid var(--line); border-radius: 12px; padding: 10px 16px;
    background: var(--panel); color: var(--text); font-weight: 650; font-size: 13px;
    display: inline-flex; align-items: center; justify-content: center; gap: 7px;
    transition: 0.15s ease; text-decoration: none;
  }
  .btn:hover { transform: translateY(-1px); border-color: var(--accent); }
  .btn:disabled { opacity: 0.45; cursor: not-allowed; transform: none; }
  .btn.primary { border-color: transparent; background: linear-gradient(135deg, var(--accent2), var(--accent)); color: #fff; box-shadow: 0 8px 18px rgba(8, 145, 178, 0.24); }
  .btn.primary:hover { filter: brightness(1.04); }
  .btn.danger { border-color: transparent; background: var(--bad); color: #fff; }
  .btn.ok { border-color: transparent; background: var(--ok); color: #fff; }
  .btn.ghost { background: var(--panel2); }
  .btn.small { padding: 8px 12px; font-size: 12px; }
  .btn.grow { flex: 1 1 auto; }
  kbd {
    font-family: inherit; font-size: 10px; font-weight: 700; line-height: 1;
    padding: 3px 5px; border-radius: 5px; border: 1px solid var(--line);
    background: var(--panel); color: var(--muted);
  }
  .spacer { flex: 1 1 auto; }
  .empty {
    border: 1px dashed var(--line); border-radius: var(--radius);
    background: var(--panel); padding: 46px 22px; text-align: center;
  }
  .empty .icon { width: 52px; height: 52px; margin: 0 auto 12px; border-radius: 16px; display: grid; place-items: center; background: var(--accent-soft); color: var(--accent); font-size: 22px; font-weight: 800; }
  .empty h3 { margin: 0 0 4px; font-size: 17px; }
  .empty p { margin: 0 auto 16px; max-width: 460px; color: var(--muted); font-size: 13px; }
  .footer {
    position: sticky; bottom: 0; z-index: 5;
    display: flex; flex-wrap: wrap; gap: 10px; align-items: center;
    margin-top: 16px; padding: 12px 0 16px;
    background: linear-gradient(180deg, transparent, var(--bg) 34%);
  }
  .status-line { color: var(--muted); font-size: 11px; }
  .status-line b { color: var(--text); }
  .footer-actions { margin-left: auto; display: flex; gap: 8px; }
  .toast-host { position: fixed; right: 16px; bottom: 16px; z-index: 60; display: flex; flex-direction: column; gap: 8px; width: min(360px, calc(100vw - 32px)); }
  .toast {
    border: 1px solid var(--line); border-left: 4px solid var(--accent);
    background: var(--panel); color: var(--text); border-radius: 12px;
    padding: 10px 12px; font-size: 12px; box-shadow: var(--shadow);
    animation: toastIn 0.18s ease;
  }
  .toast.ok { border-left-color: var(--ok); }
  .toast.warn { border-left-color: var(--warn); }
  .toast.err { border-left-color: var(--bad); }
  @keyframes toastIn { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: none; } }
  .modal { position: fixed; inset: 0; z-index: 80; display: flex; align-items: center; justify-content: center; padding: 18px; background: rgba(2, 6, 23, 0.62); backdrop-filter: blur(4px); }
  .modal-card {
    width: min(560px, 100%); max-height: 90vh; overflow: auto;
    border: 1px solid var(--line); border-radius: 18px;
    background: var(--panel); color: var(--text); box-shadow: var(--shadow);
    padding: 20px;
  }
  .modal-card h3 { margin: 0 0 4px; font-size: 18px; }
  .modal-card p { color: var(--muted); font-size: 13px; }
  .modal-actions { display: flex; gap: 8px; justify-content: flex-end; margin-top: 18px; }
  .modal-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 9px; margin: 14px 0; }
  .modal-stat { border: 1px solid var(--line); border-radius: 11px; padding: 9px 11px; background: var(--panel2); }
  .modal-stat .k { color: var(--muted); font-size: 10px; text-transform: uppercase; letter-spacing: 0.06em; }
  .modal-stat .v { font-size: 19px; font-weight: 800; }
  .notice { border-radius: 11px; padding: 10px 12px; font-size: 12px; margin: 10px 0; border: 1px solid; }
  .notice.warn { background: var(--warn-soft); color: var(--warn); border-color: var(--warn-line); }
  .notice.bad { background: var(--bad-soft); color: var(--bad); border-color: var(--bad-line); }
  .check-row { display: flex; align-items: flex-start; gap: 9px; margin-top: 12px; font-size: 13px; }
  .check-row input { margin-top: 3px; accent-color: var(--accent); }
  .kbd-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 7px 16px; margin-top: 12px; font-size: 12px; color: var(--muted); }
  .kbd-grid div { display: flex; align-items: center; gap: 7px; }
  .result { border: 1px solid var(--ok-line); border-radius: var(--radius); background: var(--panel); box-shadow: var(--shadow); padding: 22px; }
  .result-head { display: flex; align-items: center; gap: 11px; margin-bottom: 4px; }
  .result-head .badge { width: 42px; height: 42px; border-radius: 13px; background: var(--ok-soft); color: var(--ok); display: grid; place-items: center; font-weight: 800; font-size: 20px; }
  .result h2 { margin: 0; font-size: 21px; }
  .result .sub { color: var(--muted); font-size: 13px; }
  .output-list { display: grid; gap: 9px; margin: 16px 0; }
  .output-item { border: 1px solid var(--line); border-radius: 13px; background: var(--panel2); padding: 11px 13px; display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
  .output-item .name { font-weight: 700; font-size: 13px; }
  .output-item .path { width: 100%; color: var(--muted); font-family: Consolas, monospace; font-size: 11px; word-break: break-all; }
  .output-item .actions-inline { margin-left: auto; display: flex; gap: 6px; }
  .hidden { display: none !important; }
  .busy { opacity: 0.72; pointer-events: none; }
  @media (max-width: 760px) {
    .shell { padding: 16px 12px 0; }
    .stats { grid-template-columns: repeat(2, 1fr); }
    .stats .stat:last-child { grid-column: span 2; }
    .meta-grid { grid-template-columns: 1fr; }
    .search-wrap { margin-left: 0; width: 100%; flex-basis: 100%; }
    .footer-actions { width: 100%; }
    .footer-actions .btn { flex: 1; }
    .kbd-grid { grid-template-columns: 1fr; }
    .modal-grid { grid-template-columns: 1fr; }
  }
  @media (max-width: 460px) {
    .top-actions .icon-btn { width: 34px; height: 34px; }
    .card { padding: 15px; }
    .actions .btn { flex: 1 1 100%; }
    .actions .btn.small { flex: 1 1 auto; }
  }
</style>
</head>
<body>
<div class="shell">
  <header class="top">
    <div class="brand">
      <div class="logo" aria-hidden="true">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M20 10c0 6-8 12-8 12S4 16 4 10a8 8 0 1 1 16 0Z"></path>
          <path d="m9 10 2 2 4-4"></path>
        </svg>
      </div>
      <div>
        <h1>Check Cleaning</h1>
        <p class="subtitle">Review manual sebelum data dihapus</p>
      </div>
    </div>
    <div class="top-actions">
      <button class="icon-btn" id="btnTheme" onclick="toggleTheme()" title="Ganti tema" aria-label="Ganti tema">T</button>
      <button class="icon-btn" onclick="openHelp()" title="Bantuan & shortcut" aria-label="Bantuan">?</button>
      <button class="icon-btn danger" onclick="shutdown()" title="Matikan server" aria-label="Matikan server">X</button>
    </div>
  </header>

  <div class="session-bar">
    <span class="item">Input <b id="inputName">-</b></span>
    <span class="dot"></span>
    <span class="item">Brand <b id="brandName">-</b></span>
    <span class="dot"></span>
    <span class="item">Mode <b id="modeName">-</b></span>
    <span class="dot"></span>
    <span class="item" id="ruleLine">-</span>
  </div>

  <section class="stats" id="statsPanel"></section>
  <div class="progress-wrap">
    <div class="progress-track"><div class="progress-bar" id="progressBar"></div></div>
    <div class="progress-text"><span id="progressText">0 / 0 diputuskan</span><span id="progressPct">0%</span></div>
  </div>

  <section id="reviewPanel">
    <div class="toolbar">
      <div class="filters" id="filters"></div>
      <div class="search-wrap">
        <input id="search" type="search" placeholder="Cari nama, alamat, atau kategori..." autocomplete="off">
        <button class="search-clear" id="searchClear" onclick="clearSearch()" title="Bersihkan pencarian" aria-label="Bersihkan pencarian">x</button>
      </div>
    </div>
    <div id="cardHost" class="card-host"></div>
  </section>

  <section id="resultPanel" class="result hidden"></section>

  <footer id="footer" class="footer">
    <div id="statusLine" class="status-line"></div>
    <div class="footer-actions">
      <button class="btn ghost" id="btnUndo" onclick="undoDecision()">Urungkan <kbd>U</kbd></button>
      <button class="btn primary" id="btnFinalize" onclick="openFinalize()">Selesai &amp; simpan hasil</button>
    </div>
  </footer>
</div>

<div id="toastHost" class="toast-host" aria-live="polite"></div>

<div id="modalHelp" class="modal hidden" onclick="modalBackdrop(event, 'modalHelp')">
  <div class="modal-card" role="dialog" aria-modal="true" aria-labelledby="helpTitle">
    <h3 id="helpTitle">Bantuan review manual</h3>
    <p>Baris aman (nama diawali brand dan bukan blacklist) otomatis disimpan. Baris berisiko masuk daftar <b>Perlu dicek</b> dan tidak akan dihapus sampai Anda memutuskan.</p>
    <div class="kbd-grid">
      <div><kbd>Y</kbd> Simpan baris</div>
      <div><kbd>N</kbd> Hapus baris</div>
      <div><kbd>U</kbd> Urungkan keputusan terakhir</div>
      <div><kbd>O</kbd> Buka Google Maps</div>
      <div><kbd>&larr;</kbd> Baris sebelumnya</div>
      <div><kbd>&rarr;</kbd> Baris berikutnya</div>
      <div><kbd>/</kbd> Fokus ke pencarian</div>
      <div><kbd>Esc</kbd> Tutup dialog</div>
    </div>
    <div class="notice warn">Baris yang belum diputuskan saat finalisasi akan masuk ke file <b>_pending.csv</b>, bukan dihapus.</div>
    <div class="modal-actions">
      <button class="btn primary" onclick="closeModal('modalHelp')">Mengerti</button>
    </div>
  </div>
</div>

<div id="modalFinalize" class="modal hidden" onclick="modalBackdrop(event, 'modalFinalize')">
  <div class="modal-card" role="dialog" aria-modal="true" aria-labelledby="finalTitle">
    <h3 id="finalTitle">Selesaikan review?</h3>
    <p>Hasil akan disimpan sebagai CSV. Keputusan dikunci setelah finalisasi.</p>
    <div id="finalSummary" class="modal-grid"></div>
    <div id="finalNotice"></div>
    <label class="check-row">
      <input id="optCompact" type="checkbox">
      <span>Format ringkas: hasil bersih hanya 4 kolom (Name, Address, Longitude, Latitude) seperti cleaning langsung.</span>
    </label>
    <div class="modal-actions">
      <button class="btn ghost" onclick="closeModal('modalFinalize')">Batal</button>
      <button class="btn primary" id="btnDoFinalize" onclick="doFinalize()">Simpan hasil</button>
    </div>
  </div>
</div>
<script>
var S = {
  meta: null,
  stats: { total: 0, kept: 0, removed: 0, pending: 0, risky: 0, safe: 0 },
  counts: { all: 0, pending: 0, risky: 0, keep: 0, remove: 0 },
  filter: "all",
  q: "",
  pos: 0,
  row: null,
  filtered: 0,
  busy: false,
  finalized: false,
  outputs: {},
  seq: 0
};

function $(id) { return document.getElementById(id); }

function esc(value) {
  return String(value == null ? "" : value).replace(/[&<>"']/g, function (ch) {
    return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch];
  });
}

function num(value) {
  var n = Number(value || 0);
  if (!isFinite(n)) return "0";
  return n.toLocaleString("id-ID");
}

function safeUrl(value) {
  if (!value) return "";
  try {
    var url = new URL(String(value), window.location.origin);
    if (url.protocol === "http:" || url.protocol === "https:") return url.href;
  } catch (err) {}
  return "";
}

function toast(message, kind) {
  var host = $("toastHost");
  if (!host) return;
  var item = document.createElement("div");
  item.className = "toast " + (kind || "info");
  item.textContent = message;
  host.appendChild(item);
  setTimeout(function () {
    item.style.opacity = "0";
    item.style.transform = "translateY(6px)";
    item.style.transition = "0.2s ease";
    setTimeout(function () { item.remove(); }, 220);
  }, 3400);
}

async function api(path, options) {
  var response = await fetch(path, options);
  var data = null;
  try { data = await response.json(); } catch (err) {}
  if (!response.ok) {
    throw new Error((data && data.error) || ("HTTP " + response.status));
  }
  return data;
}

function post(path, body) {
  return api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {})
  });
}

function debounce(fn, delay) {
  var timer = null;
  return function () {
    var args = arguments;
    clearTimeout(timer);
    timer = setTimeout(function () { fn.apply(null, args); }, delay);
  };
}

function applyThemeButton() {
  var mode = document.documentElement.getAttribute("data-theme") || "system";
  var button = $("btnTheme");
  if (!button) return;
  button.textContent = mode === "dark" ? "G" : "T";
  button.title = mode === "dark" ? "Mode gelap (klik untuk terang)" : "Mode terang (klik untuk gelap)";
}

function toggleTheme() {
  var html = document.documentElement;
  var current = html.getAttribute("data-theme");
  var next;
  if (!current) {
    next = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "light" : "dark";
  } else {
    next = current === "dark" ? "light" : "dark";
  }
  html.setAttribute("data-theme", next);
  try { localStorage.setItem("cc-theme", next); } catch (err) {}
  applyThemeButton();
}

function renderSession() {
  var m = S.meta || {};
  $("inputName").textContent = m.input || "-";
  $("brandName").textContent = m.brand || "(tanpa brand)";
  $("modeName").textContent = m.review_all ? "semua baris" : "hanya berisiko";
  var rules = "Prefix " + (m.brand || "dilewati") + " | " +
    (m.blacklist_names || []).length + " blacklist nama | " +
    (m.blacklist_categories_count || 0) + " kategori";
  $("ruleLine").textContent = rules;
  $("ruleLine").title = (m.blacklist_names || []).join(", ");
  document.title = "Check Cleaning - " + (m.input || "review");
}

function renderStats() {
  var stats = S.stats || {};
  var boxes = [
    { label: "Total", value: stats.total, cls: "" },
    { label: "Disimpan", value: stats.kept, cls: "ok" },
    { label: "Dihapus", value: stats.removed, cls: "bad" },
    { label: "Belum", value: stats.pending, cls: "warn" },
    { label: "Berisiko", value: stats.risky, cls: "warn" }
  ];
  $("statsPanel").innerHTML = boxes.map(function (box) {
    return '<div class="stat ' + box.cls + '"><div class="label">' + box.label +
      '</div><div class="value">' + num(box.value) + '</div></div>';
  }).join("");
  var total = Number(stats.total || 0);
  var pending = Number(stats.pending || 0);
  var decided = Math.max(0, total - pending);
  var pct = total ? Math.round((decided / total) * 100) : 0;
  $("progressBar").style.width = pct + "%";
  $("progressText").textContent = num(decided) + " / " + num(total) + " diputuskan";
  $("progressPct").textContent = pct + "%";
}

function renderFilters() {
  var counts = S.counts || {};
  var items = [
    { key: "pending", label: "Perlu dicek", value: counts.pending },
    { key: "all", label: "Semua", value: counts.all },
    { key: "risky", label: "Berisiko", value: counts.risky },
    { key: "keep", label: "Disimpan", value: counts.keep },
    { key: "remove", label: "Dihapus", value: counts.remove }
  ];
  $("filters").innerHTML = items.map(function (item) {
    var active = S.filter === item.key ? " active" : "";
    return '<button class="filter-btn' + active + '" onclick="setFilter(\'' + item.key + '\')">' +
      item.label + ' <span class="count">' + num(item.value) + '</span></button>';
  }).join("");
}

function setFilter(key) {
  if (S.busy || S.finalized) return;
  S.filter = key;
  S.pos = 0;
  loadRow(0);
}

function clearSearch() {
  var input = $("search");
  if (input) input.value = "";
  S.q = "";
  S.pos = 0;
  loadRow(0);
}

function setBusy(busy) {
  S.busy = busy;
  var panel = $("reviewPanel");
  if (panel) panel.classList.toggle("busy", busy);
  var controls = document.querySelectorAll("#reviewPanel button, #btnUndo, #btnFinalize");
  controls.forEach(function (element) {
    if (element.id === "btnUndo") {
      element.disabled = busy || S.finalized || !(S.meta && S.meta.can_undo);
    } else {
      element.disabled = busy || S.finalized;
    }
  });
}

async function loadRow(pos) {
  if (S.finalized) return;
  var seq = ++S.seq;
  setBusy(true);
  try {
    var params = new URLSearchParams({
      filter: S.filter,
      q: S.q,
      pos: String(pos)
    });
    var data = await api("/api/row?" + params.toString());
    if (seq !== S.seq) return;
    S.pos = data.pos;
    S.row = data.row;
    S.filtered = data.filtered;
    S.stats = data.stats;
    S.counts = data.counts;
    S.meta.can_undo = data.can_undo;
    renderStats();
    renderFilters();
    renderCard();
    renderFooter();
  } catch (err) {
    if (seq === S.seq) toast("Gagal memuat baris: " + err.message, "err");
  } finally {
    if (seq === S.seq) setBusy(false);
  }
}

function renderCard() {
  var host = $("cardHost");
  if (!host) return;
  if (S.finalized) {
    host.innerHTML = "";
    return;
  }
  var row = S.row;
  if (!row) {
    host.innerHTML = renderEmpty();
    return;
  }
  var risks = Array.isArray(row.risk) ? row.risk : [];
  var decision = row.decision || "pending";
  var cardClass = "card " + (decision === "remove" ? "remove" : (risks.length ? "risk" : "safe"));
  var decisionChip = decision === "keep"
    ? '<span class="chip ok">Disimpan</span>'
    : (decision === "remove"
      ? '<span class="chip bad">Dihapus</span>'
      : '<span class="chip warn">Belum diputuskan</span>');
  var riskChip = risks.length
    ? '<span class="chip warn">Berisiko</span>'
    : '<span class="chip ok">Lolos filter</span>';
  var position = (S.filtered ? (S.pos + 1) : 0) + " / " + num(S.filtered);
  var maps = safeUrl(row.maps_url);
  var source = safeUrl(row.source_url);
  var riskBox = risks.length
    ? '<div class="risk-box warn"><b>Mengapa perlu dicek?</b><br>' + esc(row.reason || row.risk_label || "-") + '</div>'
    : '<div class="risk-box ok"><b>Lolos filter otomatis.</b><br>Nama diawali brand dan tidak kena blacklist. Tetap bisa diubah manual.</div>';
  var coordinate = [row.latitude, row.longitude].filter(function (v) { return String(v || "").trim() !== ""; }).join(", ");
  var links = "";
  if (maps) {
    links += '<a class="link-btn" href="' + esc(maps) + '" target="_blank" rel="noopener">Buka Google Maps <kbd>O</kbd></a>';
  }
  if (source && source !== maps) {
    links += '<a class="link-btn" href="' + esc(source) + '" target="_blank" rel="noopener">Buka Source_URL</a>';
  }

  host.innerHTML =
    '<div class="' + cardClass + '">' +
      '<div class="card-top">' +
        riskChip + decisionChip +
        '<span class="chip accent">' + esc(position) + '</span>' +
        (risks.length ? '<span class="chip">' + esc(row.risk_label || "") + '</span>' : '') +
        '<span class="spacer"></span>' +
        '<span class="chip">' + esc(row.category || "tanpa kategori") + '</span>' +
      '</div>' +
      '<div class="card-title">' + esc(row.name || "(tanpa nama)") + '</div>' +
      '<div class="card-category">' + esc(row.category || "-") + '</div>' +
      '<div class="meta-grid">' +
        '<div class="meta-box"><div class="k">Alamat</div><div class="v">' + esc(row.address || "-") + '</div></div>' +
        '<div class="meta-box"><div class="k">Koordinat</div><div class="v">' + esc(coordinate || "-") + '</div></div>' +
      '</div>' +
      riskBox +
      (links ? '<div class="link-row">' + links + '</div>' : '') +
      '<div class="actions">' +
        '<button class="btn ok grow" onclick="decide(\'keep\')">Simpan <kbd>Y</kbd></button>' +
        '<button class="btn danger grow" onclick="decide(\'remove\')">Hapus <kbd>N</kbd></button>' +
        '<button class="btn ghost" onclick="nextRow()">Lewati <kbd>&rarr;</kbd></button>' +
        '<button class="btn ghost" onclick="undoDecision()">Urungkan <kbd>U</kbd></button>' +
        '<span class="spacer"></span>' +
        '<button class="btn ghost small" onclick="prevRow()" title="Baris sebelumnya">&larr; Sebelumnya</button>' +
        '<button class="btn ghost small" onclick="nextRow()" title="Baris berikutnya">Berikutnya &rarr;</button>' +
      '</div>' +
    '</div>';
}

function renderEmpty() {
  var q = String(S.q || "").trim();
  var title = "Tidak ada baris pada filter ini";
  var message = "Coba ganti filter atau bersihkan pencarian.";
  var actions = '<button class="btn ghost" onclick="setFilter(\'all\')">Lihat semua baris</button>';
  if (q) {
    title = "Pencarian tidak menemukan hasil";
    message = "Tidak ada nama, alamat, atau kategori yang cocok dengan \"" + q + "\".";
    actions = '<button class="btn ghost" onclick="clearSearch()">Bersihkan pencarian</button>' +
      '<button class="btn ghost" onclick="setFilter(\'all\')">Lihat semua baris</button>';
  } else if (S.filter === "pending") {
    if (Number(S.counts.pending || 0) === 0) {
      title = "Semua baris sudah diputuskan";
      message = "Tidak ada lagi baris berisiko yang menunggu. Anda bisa finalisasi atau memeriksa semua baris.";
      actions = '<button class="btn ghost" onclick="setFilter(\'all\')">Lihat semua baris</button>' +
        '<button class="btn primary" onclick="openFinalize()">Selesai &amp; simpan hasil</button>';
    }
  }
  return '<div class="empty">' +
    '<div class="icon">' +
      '<svg viewBox="0 0 24 24" width="26" height="26" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
        '<circle cx="11" cy="11" r="7"></circle><path d="m20 20-4-4"></path>' +
      '</svg>' +
    '</div>' +
    '<h3>' + esc(title) + '</h3>' +
    '<p>' + esc(message) + '</p>' +
    '<div class="actions" style="justify-content:center">' + actions + '</div>' +
  '</div>';
}

function renderFooter() {
  var status = $("statusLine");
  if (!status) return;
  status.innerHTML =
    'Baris <b>' + num(S.filtered ? S.pos + 1 : 0) + '</b> dari <b>' + num(S.filtered) + '</b> pada filter ini' +
    ' &middot; ' + num(S.counts.pending) + ' belum diputuskan' +
    ' &middot; Progress tersimpan otomatis';
  var undo = $("btnUndo");
  if (undo) undo.disabled = S.busy || S.finalized || !(S.meta && S.meta.can_undo);
  var finalize = $("btnFinalize");
  if (finalize) finalize.disabled = S.busy || S.finalized;
}
async function decide(decision) {
  if (S.busy || S.finalized || !S.row) return;
  var row = S.row;
  var risks = Array.isArray(row.risk) ? row.risk : [];
  if (decision === "remove" && !risks.length) {
    var ok = window.confirm(
      "Baris ini lolos filter otomatis (nama diawali brand dan tidak kena blacklist).\n\n" +
      "Nama: " + (row.name || "(tanpa nama)") + "\n\nTetap hapus baris ini?"
    );
    if (!ok) return;
  }
  var seq = ++S.seq;
  setBusy(true);
  try {
    var data = await post("/api/decide", {
      row_id: row.row_id,
      decision: decision,
      filter: S.filter,
      q: S.q,
      pos: S.pos
    });
    if (seq !== S.seq) return;
    S.pos = data.pos;
    S.row = data.row;
    S.filtered = data.filtered;
    S.stats = data.stats;
    S.counts = data.counts;
    S.meta.can_undo = data.can_undo;
    renderStats();
    renderFilters();
    renderCard();
    renderFooter();
    toast(
      (decision === "keep" ? "Disimpan: " : "Dihapus: ") + (row.name || "(tanpa nama)"),
      decision === "keep" ? "ok" : "warn"
    );
    if (!data.saved && data.progress_error) toast(data.progress_error, "err");
  } catch (err) {
    if (seq === S.seq) toast("Gagal menyimpan keputusan: " + err.message, "err");
  } finally {
    if (seq === S.seq) setBusy(false);
  }
}

async function undoDecision() {
  if (S.busy || S.finalized) return;
  var seq = ++S.seq;
  setBusy(true);
  try {
    var data = await post("/api/undo", {
      filter: S.filter,
      q: S.q,
      pos: S.pos
    });
    if (seq !== S.seq) return;
    S.pos = data.pos;
    S.row = data.row;
    S.filtered = data.filtered;
    S.stats = data.stats;
    S.counts = data.counts;
    S.meta.can_undo = data.can_undo;
    renderStats();
    renderFilters();
    renderCard();
    renderFooter();
    toast("Keputusan terakhir diurungkan.", "info");
  } catch (err) {
    if (seq === S.seq) toast(err.message, "warn");
  } finally {
    if (seq === S.seq) setBusy(false);
  }
}

function nextRow() {
  if (S.busy || S.finalized) return;
  if (!S.row) {
    loadRow(0);
    return;
  }
  if (S.pos + 1 >= S.filtered) {
    toast("Sudah di baris terakhir pada filter ini.", "info");
    return;
  }
  loadRow(S.pos + 1);
}

function prevRow() {
  if (S.busy || S.finalized) return;
  if (!S.row) {
    loadRow(0);
    return;
  }
  if (S.pos <= 0) {
    toast("Sudah di baris pertama pada filter ini.", "info");
    return;
  }
  loadRow(S.pos - 1);
}

function openMaps() {
  if (!S.row) return;
  var url = safeUrl(S.row.maps_url);
  if (!url) {
    toast("Link Google Maps tidak tersedia untuk baris ini.", "warn");
    return;
  }
  window.open(url, "_blank", "noopener");
}

function openFinalize() {
  if (S.busy || S.finalized) return;
  var stats = S.stats || {};
  var pending = Number(stats.pending || 0);
  var kept = Number(stats.kept || 0);
  $("finalSummary").innerHTML =
    '<div class="modal-stat"><div class="k">Disimpan</div><div class="v" style="color:var(--ok)">' + num(kept) + '</div></div>' +
    '<div class="modal-stat"><div class="k">Dihapus</div><div class="v" style="color:var(--bad)">' + num(stats.removed) + '</div></div>' +
    '<div class="modal-stat"><div class="k">Belum diputuskan</div><div class="v" style="color:var(--warn)">' + num(pending) + '</div></div>' +
    '<div class="modal-stat"><div class="k">Total</div><div class="v">' + num(stats.total) + '</div></div>';
  var notice = "";
  if (pending) {
    notice = '<div class="notice warn">Masih ada <b>' + num(pending) + '</b> baris belum diputuskan. Baris tersebut akan masuk ke file <b>_pending.csv</b> dan <b>tidak dihapus</b>.</div>';
  }
  if (!kept) {
    notice += '<div class="notice bad"><b>Tidak ada baris yang disimpan.</b> Pastikan ini memang disengaja sebelum melanjutkan.</div>';
  }
  $("finalNotice").innerHTML = notice;
  $("optCompact").checked = !!(S.meta && S.meta.compact_default);
  openModal("modalFinalize");
}

async function doFinalize() {
  var button = $("btnDoFinalize");
  if (S.finalized) return;
  if (Number(S.stats.kept || 0) === 0) {
    if (!window.confirm("Tidak ada baris yang akan disimpan. Lanjutkan finalisasi?")) return;
  }
  button.disabled = true;
  button.textContent = "Menyimpan...";
  try {
    var compact = !!$("optCompact").checked;
    var data = await post("/api/finalize", {
      compact: compact,
      allow_pending: true
    });
    S.finalized = true;
    S.outputs = data.outputs || {};
    S.stats = data.stats || S.stats;
    S.counts = data.counts || S.counts;
    closeModal("modalFinalize");
    renderStats();
    renderFilters();
    showResult(data);
    toast("Review selesai. File hasil sudah dibuat.", "ok");
    if (!data.saved && data.progress_error) toast(data.progress_error, "err");
  } catch (err) {
    toast("Gagal finalisasi: " + err.message, "err");
    button.disabled = false;
    button.textContent = "Simpan hasil";
  }
}

function showResult(data) {
  $("reviewPanel").classList.add("hidden");
  $("footer").classList.add("hidden");
  var panel = $("resultPanel");
  panel.classList.remove("hidden");
  var outputs = (data && data.outputs) || S.outputs || {};
  var stats = (data && data.stats) || S.stats;
  var order = ["clean", "removed", "pending", "review"];
  var labels = {
    clean: "Data bersih (disimpan)",
    removed: "Data dihapus",
    pending: "Belum diputuskan",
    review: "Audit keputusan"
  };
  var items = order.filter(function (key) { return outputs[key]; }).map(function (key) {
    var info = outputs[key] || {};
    return '<div class="output-item">' +
      '<div class="name">' + esc(labels[key] || key) + '</div>' +
      '<div class="actions-inline">' +
        '<a class="btn primary small" href="' + esc(info.url || "#") + '">Unduh</a>' +
        '<button class="btn ghost small" data-copy="' + esc(info.path || "") + '" onclick="copyFromButton(this)">Salin path</button>' +
      '</div>' +
      '<div class="path">' + esc(info.path || "") + '</div>' +
    '</div>';
  }).join("");
  panel.innerHTML =
    '<div class="result-head">' +
      '<div class="badge">OK</div>' +
      '<div><h2>Review selesai</h2><div class="sub">Keputusan sudah dikunci. File tersimpan di folder output.</div></div>' +
    '</div>' +
    '<div class="modal-grid">' +
      '<div class="modal-stat"><div class="k">Disimpan</div><div class="v" style="color:var(--ok)">' + num(stats.kept) + '</div></div>' +
      '<div class="modal-stat"><div class="k">Dihapus</div><div class="v" style="color:var(--bad)">' + num(stats.removed) + '</div></div>' +
      '<div class="modal-stat"><div class="k">Pending</div><div class="v" style="color:var(--warn)">' + num(stats.pending) + '</div></div>' +
      '<div class="modal-stat"><div class="k">Total</div><div class="v">' + num(stats.total) + '</div></div>' +
    '</div>' +
    '<div class="output-list">' + (items || '<div class="notice warn">Tidak ada file output yang ditemukan.</div>') + '</div>' +
    '<div class="notice warn">Untuk mengulang review dari awal, jalankan ulang skrip dengan opsi <b>--reset</b>.</div>' +
    '<div class="actions">' +
      '<button class="btn danger" onclick="shutdown()">Matikan server</button>' +
    '</div>';
}

function copyFromButton(button) {
  if (!button) return;
  copyText(button.getAttribute("data-copy") || "");
}

function copyText(text) {
  if (!text) return;
  var done = function () { toast("Path disalin.", "ok"); };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done).catch(function () { fallbackCopy(text, done); });
  } else {
    fallbackCopy(text, done);
  }
}

function fallbackCopy(text, done) {
  var area = document.createElement("textarea");
  area.value = text;
  area.style.position = "fixed";
  area.style.opacity = "0";
  document.body.appendChild(area);
  area.select();
  try { document.execCommand("copy"); done(); } catch (err) { toast("Gagal menyalin path.", "err"); }
  area.remove();
}

function openModal(id) {
  var modal = $(id);
  if (!modal) return;
  modal.classList.remove("hidden");
  var focusable = modal.querySelector("button, input");
  if (focusable) setTimeout(function () { focusable.focus(); }, 30);
}

function closeModal(id) {
  var modal = $(id);
  if (modal) modal.classList.add("hidden");
}

function modalBackdrop(event, id) {
  if (event.target === $(id)) closeModal(id);
}

function openHelp() {
  openModal("modalHelp");
}

function closeAllModals() {
  document.querySelectorAll(".modal").forEach(function (modal) {
    modal.classList.add("hidden");
  });
}

async function shutdown() {
  if (!window.confirm("Matikan server review? Progress dan keputusan sudah tersimpan.")) return;
  try { await post("/api/shutdown", {}); } catch (err) {}
  document.body.innerHTML =
    '<div style="min-height:100vh;display:grid;place-items:center;padding:24px;text-align:center;font-family:Segoe UI,system-ui,sans-serif">' +
      '<div><h2>Server review sudah dimatikan</h2>' +
      '<p style="color:#64748b">Anda boleh menutup tab ini. File hasil tetap tersimpan di folder output.</p></div>' +
    '</div>';
}

function showFatal(err) {
  $("cardHost").innerHTML =
    '<div class="empty"><div class="icon">!</div><h3>Tidak bisa memuat sesi review</h3>' +
    '<p>' + esc(err.message) + '</p>' +
    '<button class="btn ghost" onclick="location.reload()">Muat ulang</button></div>';
}

document.addEventListener("keydown", function (event) {
  if (event.key === "Escape") {
    closeAllModals();
    return;
  }
  var tag = (document.activeElement && document.activeElement.tagName || "").toUpperCase();
  var typing = tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
  if (typing) return;
  if (event.ctrlKey || event.metaKey || event.altKey) return;
  var key = String(event.key || "").toLowerCase();
  if (event.key === "?") {
    event.preventDefault();
    openHelp();
  } else if (key === "y") {
    event.preventDefault();
    decide("keep");
  } else if (key === "n") {
    event.preventDefault();
    decide("remove");
  } else if (key === "u") {
    event.preventDefault();
    undoDecision();
  } else if (key === "o") {
    event.preventDefault();
    openMaps();
  } else if (event.key === "/" || key === "f") {
    event.preventDefault();
    $("search").focus();
  } else if (event.key === "ArrowRight") {
    event.preventDefault();
    nextRow();
  } else if (event.key === "ArrowLeft") {
    event.preventDefault();
    prevRow();
  }
});

var onSearch = debounce(function () {
  if (S.busy || S.finalized) return;
  S.q = String($("search").value || "").trim();
  S.pos = 0;
  loadRow(0);
}, 260);

async function init() {
  applyThemeButton();
  $("search").addEventListener("input", onSearch);
  try {
    var data = await api("/api/state");
    S.meta = data.meta || {};
    S.stats = data.stats || S.stats;
    S.counts = data.counts || S.counts;
    S.finalized = !!S.meta.finalized;
    S.outputs = data.outputs || {};
    S.filter = data.default_filter || "all";
    renderSession();
    renderStats();
    renderFilters();
    if (S.finalized) {
      showResult({ outputs: S.outputs, stats: S.stats });
      return;
    }
    await loadRow(0);
    if (S.meta.resumed) toast("Progress sebelumnya berhasil dimuat.", "ok");
    if (S.meta.progress_error) toast(S.meta.progress_error, "warn");
  } catch (err) {
    showFatal(err);
  }
}

init();
</script>
</body>
</html>
"""


def make_handler(session):
    html_bytes = HTML.encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        server_version = "CheckCleaning/1.0"
        protocol_version = "HTTP/1.1"

        def send_json(self, payload, status=200):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def send_html(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html_bytes)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(html_bytes)

        def send_download(self, key):
            outputs = session.public_outputs()
            info = outputs.get(key)
            if not info:
                self.send_json({"error": "File tidak ditemukan."}, 404)
                return
            path = Path(info["path"])
            if not path.is_file():
                self.send_json({"error": "File sudah tidak ada."}, 404)
                return
            body = path.read_bytes()
            name = info["name"]
            ascii_name = "".join(ch if ord(ch) < 128 else "_" for ch in name) or "download.csv"
            disposition = (
                f'attachment; filename="{ascii_name}"; '
                f"filename*=UTF-8''{quote(name)}"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Disposition", disposition)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parsed = urlparse(self.path)
            try:
                if parsed.path in ("/", "/index.html"):
                    self.send_html()
                    return
                if parsed.path == "/favicon.ico":
                    self.send_response(204)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if parsed.path == "/api/state":
                    self.send_json(session.state_payload())
                    return
                if parsed.path == "/api/row":
                    query = parse_qs(parsed.query)
                    filter_key = query.get("filter", ["all"])[0]
                    search = query.get("q", [""])[0]
                    pos = query.get("pos", ["0"])[0]
                    self.send_json(session.current(filter_key, search, pos))
                    return
                if parsed.path.startswith("/download/"):
                    self.send_download(parsed.path.rsplit("/", 1)[-1])
                    return
                self.send_json({"error": "Halaman tidak ditemukan."}, 404)
            except Exception as exc:
                self.send_json({"error": str(exc)}, 500)

        def do_POST(self):
            parsed = urlparse(self.path)
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length > 1024 * 1024:
                    self.send_json({"error": "Permintaan terlalu besar."}, 413)
                    return
                raw = self.rfile.read(length) if length else b"{}"
                if not raw.strip():
                    raw = b"{}"
                try:
                    data = json.loads(raw.decode("utf-8"))
                except Exception:
                    self.send_json({"error": "JSON tidak valid."}, 400)
                    return
                if not isinstance(data, dict):
                    self.send_json({"error": "Payload harus JSON object."}, 400)
                    return

                if parsed.path == "/api/decide":
                    payload = session.decide(
                        data.get("row_id"),
                        data.get("decision"),
                        data.get("filter", "all"),
                        data.get("q", ""),
                        data.get("pos", 0),
                    )
                    self.send_json(payload)
                    return
                if parsed.path == "/api/undo":
                    payload = session.undo(
                        data.get("filter", "all"),
                        data.get("q", ""),
                        data.get("pos", 0),
                    )
                    self.send_json(payload)
                    return
                if parsed.path == "/api/finalize":
                    payload = session.finalize(
                        compact=bool(data.get("compact")),
                        allow_pending=bool(data.get("allow_pending")),
                    )
                    self.send_json(payload)
                    return
                if parsed.path == "/api/shutdown":
                    self.send_json({"ok": True})
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                    return
                self.send_json({"error": "Aksi tidak ditemukan."}, 404)
            except ValueError as exc:
                self.send_json({"error": str(exc)}, 400)
            except Exception as exc:
                self.send_json({"error": str(exc)}, 500)

        def log_message(self, format, *args):
            pass

    return Handler


def start_server(session, args):
    def build_server(port):
        return ThreadingHTTPServer((args.host, port), make_handler(session))

    try:
        server = build_server(args.port)
    except OSError:
        if args.port == 0:
            raise
        print(f"Port {args.port} tidak bisa dipakai, memilih port acak.")
        server = build_server(0)

    server.daemon_threads = True
    actual_port = server.server_address[1]
    display_host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    url = f"http://{display_host}:{actual_port}/"

    print("\n===================================")
    print(f"Review web     : {url}")
    print("Tekan Ctrl+C untuk menghentikan server.")
    print("Shortcut       : Y simpan, N hapus, U undo, O buka Maps, panah navigasi.")
    print("===================================")

    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nMenghentikan server...")
    finally:
        session.save_progress()
        server.server_close()
        print("Server dihentikan. Progress tersimpan.")


def main():
    configure_stdout()
    parser = build_parser()
    args = parser.parse_args()
    try:
        return run(args)
    except KeyboardInterrupt:
        print("\nDibatalkan.")
        return 130
    except Exception as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

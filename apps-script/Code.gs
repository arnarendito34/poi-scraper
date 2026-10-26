/**
 * POI Scraper - Apps Script API + Dashboard
 * Menghubungkan Google Sheet (DB) + GitHub Actions (worker).
 *
 * Deploy sebagai Web App (execute as: me, access: anyone).
 */

var JOB_HEADERS = [
  "job_id", "user", "country", "brand", "spec_json", "status", "progress",
  "created_at", "started_at", "finished_at", "output_file_id", "output_url", "error",
  "result_sheet", "result_gid", "current_target", "listings_found", "records", "eta", "run_url",
  "current_chunk", "total_chunks", "overall_progress", "chunks_json", "filter_stats_json",
  "run_id", "control", "checkpoint_json", "cancelled_at", "zero_targets_json"
];
var USER_HEADERS = [
  "email", "api_key_hash", "quota", "used", "active", "created_at", "api_key"
];
var LOG_HEADERS = ["timestamp", "job_id", "user", "message"];
var CLEAN_HEADERS = [
  "clean_id", "user", "brand", "source_type", "source_name", "source_job",
  "status", "total", "safe", "risky", "kept", "removed", "pending",
  "tab", "tab_gid", "final_tab", "final_gid", "created_at", "finished_at", "csv_url"
];
var CLEAN_REVIEW_HEADERS = [
  "_row_id", "_decision", "_risk", "_reason",
  "Name_geocode", "Address_geocode", "Longitude_geocode", "Latitude_geocode",
  "Category_geocode", "Source_URL"
];
var CLEAN_OUTPUT_COLUMNS = [
  "Name_geocode", "Address_geocode", "Longitude_geocode", "Latitude_geocode", "Category_geocode"
];
var CLEAN_BLACKLIST_NAMES = [
  "Jaya"
];
var CLEAN_BLACKLIST_CATEGORIES = [
  "Corporate office", "Bakery", "Bus stop", "ATM", "Dessert shop", "Coffee shop",
  "Tea store", "Parking lot", "Deli", "Vaporizer store", "Employment agency",
  "Restaurant", "Fast food restaurant", "Ice cream shop", "Coffee stand",
  "Air compressor supplier", "Mine", "Vegetable wholesaler", "Logistics service",
  "Company", "General contractor", "Public parking space",
  "Non-governmental organization", "Air conditioning store",
  "Construction company", "Pharmacy", "Vegetarian restaurant",
  "Night club", "Tea house", "Bubble tea store", "Juice shop",
  "Ranch", "Electronics store", "Office supply store",
  "Beverage distributor", "Pest control service",
  "Nasi goreng restaurant", "Porridge restaurant",
  "Auto machine shop", "Freight forwarding service",
  "Display stand manufacturer", "Hawker stall",
  "Distribution service", "Electric motorcycle dealer",
  "Health food store", "Saw mill", "Chocolate cafe",
  "Brunch restaurant", "Fish store", "Tofu shop",
  "Computer repair service", "Stationery store",
  "Feed manufacturer", "Cigar shop", "Bank",
  "Hawker center"
];
var APP_VERSION = "2026-10-26.5";

// ------------------------------------------------------------
// Properties & Sheet helpers
// ------------------------------------------------------------
function props_() {
  return PropertiesService.getScriptProperties().getProperties();
}

function prop_(key, fallback) {
  var v = PropertiesService.getScriptProperties().getProperty(key);
  return v === null || v === "" ? (fallback || "") : v;
}

function sheet_() {
  var id = prop_("SHEET_ID");
  if (!id) throw new Error("SHEET_ID belum diset di Script Properties.");
  return SpreadsheetApp.openById(id);
}

function tab_(name, headers) {
  var ss = sheet_();
  var sh = ss.getSheetByName(name);
  if (!sh) {
    sh = ss.insertSheet(name);
    sh.appendRow(headers);
  }
  return sh;
}

function readTable_(name, headers) {
  var sh = tab_(name, headers);
  var values = sh.getDataRange().getValues();
  if (!values.length) return [];
  var head = values.shift().map(function (h) { return String(h).trim(); });
  return values.filter(function (r) { return String(r[0]).length > 0; }).map(function (r) {
    var o = {};
    head.forEach(function (h, i) {
      var v = (i < r.length) ? r[i] : "";
      o[h] = (v === null || v === undefined) ? "" : v;
    });
    return o;
  });
}

function sanitizeJob_(job) {
  var out = {};
  Object.keys(job).forEach(function (k) {
    var v = job[k];
    if (v instanceof Date) out[k] = fmtVal_(v);
    else out[k] = (v === null || v === undefined) ? "" : v;
  });
  return out;
}

function nowStr_() {
  return Utilities.formatDate(new Date(), "Asia/Jakarta", "yyyy-MM-dd HH:mm:ss");
}

function fmtVal_(v) {
  if (v instanceof Date) return Utilities.formatDate(v, "Asia/Jakarta", "yyyy-MM-dd HH:mm:ss");
  return v === null || v === undefined ? "" : String(v);
}

function hashKey_(key) {
  var bytes = Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, String(key));
  return bytes.map(function (b) { return ("0" + (b & 0xff).toString(16)).slice(-2); }).join("");
}

// ------------------------------------------------------------
// Users & auth
// ------------------------------------------------------------
function findUserByEmail_(email) {
  email = String(email || "").toLowerCase();
  var users = readTable_("Users", USER_HEADERS);
  for (var i = 0; i < users.length; i++) {
    if (String(users[i].email).toLowerCase() === email) return users[i];
  }
  return null;
}

function findUserByKey_(key) {
  var h = hashKey_(key);
  var users = readTable_("Users", USER_HEADERS);
  for (var i = 0; i < users.length; i++) {
    if (String(users[i].api_key_hash) === h) return users[i];
  }
  return null;
}

function appendUser_(email, keyHash, quota, active, apiKey) {
  var sh = tab_("Users", USER_HEADERS);
  sh.appendRow([email, keyHash, quota, 0, active, nowStr_(), apiKey || ""]);
  return { email: email, api_key_hash: keyHash, quota: quota, used: 0, active: active, api_key: apiKey || "" };
}

function verifyGoogleToken_(idToken) {
  var cache = CacheService.getScriptCache();
  var cacheKey = "tok_" + hashKey_(idToken).slice(0, 40);
  var cached = cache.get(cacheKey);
  if (cached) return cached;

  var res = UrlFetchApp.fetch(
    "https://oauth2.googleapis.com/tokeninfo?id_token=" + encodeURIComponent(idToken),
    { muteHttpExceptions: true }
  );
  if (res.getResponseCode() !== 200) throw new Error("Token Google tidak valid.");
  var info = JSON.parse(res.getContentText());
  var clientId = prop_("OAUTH_CLIENT_ID");
  if (clientId && info.aud !== clientId) throw new Error("Audience token Google tidak cocok.");
  if (!info.email) throw new Error("Token Google tidak memuat email.");
  cache.put(cacheKey, info.email, 1800);
  return info.email;
}

function resolveIdentity_(ident) {
  ident = ident || {};
  var email;
  if (ident.idToken) {
    email = verifyGoogleToken_(ident.idToken);
  } else if (ident.apiKey) {
    var byKey = findUserByKey_(ident.apiKey);
    if (!byKey) throw new Error("API key tidak valid. Jika tab Users masih kosong, jalankan bootstrapAdmin() di editor Apps Script untuk membuat key pertama.");
    email = byKey.email;
  } else {
    throw new Error("Autentikasi diperlukan (id_token atau api_key).");
  }

  var user = findUserByEmail_(email);
  if (!user) {
    if (ident.idToken) {
      user = appendUser_(email, "", Number(prop_("DEFAULT_QUOTA", "0")), true, "");
    } else {
      throw new Error("User tidak terdaftar.");
    }
  }
  if (String(user.active).toUpperCase() !== "TRUE") throw new Error("Akun nonaktif.");
  return user;
}

function checkQuota_(user) {
  var quota = Number(user.quota || 0);
  var used = Number(user.used || 0);
  if (quota > 0 && used >= quota) throw new Error("Kuota habis (" + used + "/" + quota + ").");
}

function incrementUsed_(email) {
  var sh = tab_("Users", USER_HEADERS);
  var values = sh.getDataRange().getValues();
  for (var i = 1; i < values.length; i++) {
    if (String(values[i][0]).toLowerCase() === String(email).toLowerCase()) {
      sh.getRange(i + 1, 4).setValue(Number(values[i][3] || 0) + 1);
      return;
    }
  }
}

// ------------------------------------------------------------
// Jobs
// ------------------------------------------------------------
function createJob_(user, payload) {
  checkQuota_(user);
  var country = String(payload.country || "indonesia").toLowerCase();
  var brandRaw = String(payload.brand || "").trim();
  if (!brandRaw) throw new Error("Brand wajib diisi.");
  if (country !== "indonesia" && country !== "philippines") throw new Error("Negara tidak dikenal.");
  var aliasList = brandRaw.split("|").map(function (s) { return s.trim(); })
    .filter(function (s) { return s.length > 0; });
  var brand = aliasList[0] || brandRaw;

  var rawParams = payload.params || {};
  var relMode = String(rawParams.relevance_mode || "mirip").toLowerCase();
  if (["persis", "mirip", "longgar"].indexOf(relMode) < 0) relMode = "mirip";
  var params = {
    filter_relevance: rawParams.filter_relevance !== false,
    relevance_mode: relMode,
    keep_no_coords: rawParams.keep_no_coords !== false,
    headless: true
  };
  var radius = Number(rawParams.radius_km);
  if (!isNaN(radius) && radius > 0) params.radius_km = Math.max(1, Math.min(300, radius));

  var jobId = Utilities.getUuid();
  var resultTab = makeResultTab_(brand, jobId);
  var spec = {
    job_id: jobId,
    user: user.email,
    country: country,
    brand: brand,
    level: payload.level || "",
    scope: payload.scope || {},
    mode: payload.mode || "unit",
    tile: Number(payload.tile || 0),
    result_sheet: resultTab.name,
    params: params
  };
  if (aliasList.length > 1) {
    spec.aliases = aliasList;
  }
  if (payload.explicit_admins && payload.explicit_admins.length) {
    spec.explicit_admins = payload.explicit_admins;
  }

  tab_("Jobs", JOB_HEADERS).appendRow([
    jobId, user.email, country, brand, JSON.stringify(spec),
    "queued", "0%", nowStr_(), "", "", "", "", "",
    resultTab.name, resultTab.gid, "", 0, 0, "", "",
    "", 0, 0, "{}", "{}", "", "run", "", "", "[]"
  ]);
  incrementUsed_(user.email);
  try { dispatch(); } catch (e) { log_(jobId, user.email, "dispatch error: " + e); }
  return { job_id: jobId, status: "queued", result_sheet: resultTab.name };
}

function makeResultTab_(brand, jobId) {
  var ss = sheet_();
  var stamp = Utilities.formatDate(new Date(), "Asia/Jakarta", "yyyyMMdd_HHmm");
  var safe = String(brand).replace(/[^A-Za-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 20) || "result";
  var name = "Result_" + safe + "_" + stamp + "_" + String(jobId).slice(0, 8);
  var sh = ss.insertSheet(name);
  return { name: name, gid: sh.getSheetId() };
}

function getJob_(jobId) {
  var jobs = readTable_("Jobs", JOB_HEADERS);
  for (var i = 0; i < jobs.length; i++) {
    if (String(jobs[i].job_id) === String(jobId)) return sanitizeJob_(jobs[i]);
  }
  return null;
}

function listJobs_(user) {
  var jobs = readTable_("Jobs", JOB_HEADERS);
  var admin = isAdmin_(user.email);
  var out = jobs.filter(function (j) {
    return admin || String(j.user).toLowerCase() === String(user.email).toLowerCase();
  });
  out.sort(function (a, b) { return String(b.created_at).localeCompare(String(a.created_at)); });
  var sheetId = prop_("SHEET_ID");
  return out.slice(0, 100).map(function (j) {
    var gid = j.result_gid;
    var tabUrl = (j.result_sheet && gid !== "" && gid !== null && gid !== undefined)
      ? "https://docs.google.com/spreadsheets/d/" + sheetId + "/edit#gid=" + gid
      : "";
    return {
      job_id: j.job_id || "", user: j.user || "", country: j.country || "", brand: j.brand || "",
      status: j.status || "", progress: j.progress || "", created_at: fmtVal_(j.created_at),
      finished_at: fmtVal_(j.finished_at), output_url: j.output_url || "", error: fmtVal_(j.error),
      current_target: j.current_target || "", listings_found: j.listings_found || 0,
      records: j.records || 0, eta: j.eta || "", run_url: j.run_url || "",
      result_sheet: j.result_sheet || "", result_url: tabUrl,
      current_chunk: j.current_chunk || "", total_chunks: Number(j.total_chunks || 0),
      overall_progress: Number(j.overall_progress || 0), chunks_json: j.chunks_json || "",
      filter_stats_json: j.filter_stats_json || ""
    };
  });
}

function diag_() {
  var ss = sheet_();
  var tabs = ss.getSheets().map(function (s) { return s.getName(); });
  function header(name) {
    var sh = ss.getSheetByName(name);
    if (!sh) return null;
    var cols = Math.max(1, sh.getLastColumn());
    return sh.getRange(1, 1, 1, cols).getValues()[0].map(function (v) { return String(v); });
  }
  var jobs = readTable_("Jobs", JOB_HEADERS);
  var users = readTable_("Users", USER_HEADERS);
  return {
    sheet_id: prop_("SHEET_ID"),
    max_parallel: prop_("MAX_PARALLEL"),
    admin_emails: prop_("ADMIN_EMAILS"),
    tabs: tabs,
    jobs_header: header("Jobs"),
    users_header: header("Users"),
    cleanings_header: header("Cleanings"),
    jobs_count: jobs.length,
    users_count: users.length,
    cleanings_count: readTable_("Cleanings", CLEAN_HEADERS).length
  };
}

function canAccessJob_(user, job) {
  if (!job) return false;
  return isAdmin_(user.email) || String(job.user).toLowerCase() === String(user.email).toLowerCase();
}

function extractRunId_(url) {
  var m = String(url || "").match(/\/runs\/(\d+)/);
  return m ? m[1] : "";
}

function githubCancelRun_(runId) {
  var owner = prop_("GITHUB_OWNER");
  var repo = prop_("GITHUB_REPO");
  var pat = prop_("GITHUB_PAT");
  if (!owner || !repo || !pat) throw new Error("Konfigurasi GitHub belum lengkap.");
  var url = "https://api.github.com/repos/" + owner + "/" + repo + "/actions/runs/" + runId + "/cancel";
  var res = UrlFetchApp.fetch(url, {
    method: "post",
    headers: {
      Authorization: "Bearer " + pat,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28"
    },
    muteHttpExceptions: true
  });
  if (res.getResponseCode() >= 300) {
    throw new Error("GitHub cancel gagal (" + res.getResponseCode() + "): " + res.getContentText());
  }
}

function jobControl_(user, payload, action) {
  var job = getJob_(payload.job_id);
  if (!job) throw new Error("Job tidak ditemukan.");
  if (!canAccessJob_(user, job)) throw new Error("Akses ditolak.");
  var status = String(job.status || "").toLowerCase();

  if (action === "pause") {
    if (status !== "running" && status !== "dispatched") throw new Error("Job tidak sedang berjalan.");
    setJobField_(job.job_id, "control", "pause");
    log_(job.job_id, job.user, "Pause diminta oleh " + user.email);
    return { job_id: job.job_id, status: "pausing" };
  }

  if (action === "resume") {
    if (status !== "paused" && status !== "cancelled" && status !== "failed") {
      throw new Error("Hanya job paused/cancelled/failed yang bisa dilanjutkan.");
    }
    var spec = JSON.parse(job.spec_json);
    var chunks = {};
    try { chunks = JSON.parse(job.chunks_json || "{}") || {}; } catch (e) { chunks = {}; }
    var done = [];
    Object.keys(chunks).forEach(function (k) {
      var cs = String((chunks[k] || {}).status || "").toLowerCase();
      if (cs === "done" && chunks[k].label) done.push(chunks[k].label);
    });
    if (done.length) spec.resume_done_chunks = done;
    setJobField_(job.job_id, "control", "run");
    setJobField_(job.job_id, "spec_json", JSON.stringify(spec));
    setJobField_(job.job_id, "status", "queued");
    setJobField_(job.job_id, "error", "");
    log_(job.job_id, job.user, "Resume diminta oleh " + user.email + " (lewati " + done.length + " chunk selesai)");
    dispatch();
    return { job_id: job.job_id, status: "queued" };
  }

  if (action === "cancel") {
    if (status === "done") throw new Error("Job sudah selesai; tidak perlu dibatalkan.");
    if (status === "cancelled") return { job_id: job.job_id, status: "cancelled" };
    var runId = String(job.run_id || "") || extractRunId_(job.run_url);
    if (runId) {
      try { githubCancelRun_(runId); }
      catch (e) { log_(job.job_id, job.user, "cancel API gagal: " + e); }
    }
    setJobField_(job.job_id, "control", "stop");
    setJobField_(job.job_id, "status", "cancelled");
    setJobField_(job.job_id, "cancelled_at", nowStr_());
    log_(job.job_id, job.user, "Cancel diminta oleh " + user.email);
    return { job_id: job.job_id, status: "cancelled" };
  }

  throw new Error("Aksi tidak dikenal: " + action);
}

function deleteJobTab_(tabName) {
  tabName = String(tabName || "");
  if (!tabName || ["Users", "Jobs", "Logs", "Cleanings"].indexOf(tabName) >= 0) return false;
  var ss = sheet_();
  var sh = ss.getSheetByName(tabName);
  if (!sh) return false;
  ss.deleteSheet(sh);
  return true;
}

function driveIdFromUrl_(url) {
  url = String(url || "");
  if (url.indexOf("docs.google.com/spreadsheets") >= 0) return "";
  var m = url.match(/\/d\/([A-Za-z0-9_-]+)/);
  return m ? m[1] : "";
}

function deleteRowsById_(name, headers, idField, jobId) {
  var sh = tab_(name, headers);
  var values = sh.getDataRange().getValues();
  if (values.length < 2) return 0;
  var head = values[0].map(function (h) { return String(h).trim(); });
  var col = head.indexOf(idField);
  if (col < 0) col = 0;
  var rows = [];
  for (var i = 1; i < values.length; i++) {
    if (String(values[i][col] || "") === String(jobId)) rows.push(i + 1);
  }
  if (!rows.length) return 0;

  // Logs bisa ribuan baris; tulis ulang sekali agar tidak kena batas 6 menit.
  if (name === "Logs" && rows.length > 200) {
    var keep = [values[0]];
    for (var j = 1; j < values.length; j++) {
      if (String(values[j][col] || "") !== String(jobId)) keep.push(values[j]);
    }
    sh.clearContents();
    sh.getRange(1, 1, keep.length, values[0].length).setValues(keep);
    return rows.length;
  }

  // Hapus blok baris berurutan dari bawah (hemat panggilan deleteRows).
  var deleted = 0;
  var k = rows.length - 1;
  while (k >= 0) {
    var end = rows[k];
    var start = end;
    k--;
    while (k >= 0 && rows[k] === start - 1) {
      start = rows[k];
      k--;
    }
    sh.deleteRows(start, end - start + 1);
    deleted += end - start + 1;
  }
  return deleted;
}

function deleteJob_(user, payload) {
  var job = getJob_(payload.job_id);
  if (!job) throw new Error("Job tidak ditemukan.");
  if (!canAccessJob_(user, job)) throw new Error("Akses ditolak.");
  var status = String(job.status || "").toLowerCase();
  if (status === "queued" || status === "running" || status === "dispatched" || status === "pausing") {
    throw new Error("Job masih aktif (status " + status + "). Hentikan dulu sebelum menghapus.");
  }

  var warnings = [];
  var tabDeleted = false;
  try { tabDeleted = deleteJobTab_(job.result_sheet); }
  catch (e) { warnings.push("tab hasil: " + e); }

  var fileDeleted = false;
  var fileId = String(job.output_file_id || "") || driveIdFromUrl_(job.output_url);
  if (fileId && fileId !== prop_("SHEET_ID")) {
    try { DriveApp.getFileById(fileId).setTrashed(true); fileDeleted = true; }
    catch (e) { warnings.push("file CSV: " + e); }
  }

  var logsDeleted = 0;
  try { logsDeleted = deleteRowsById_("Logs", LOG_HEADERS, "job_id", job.job_id); }
  catch (e) { warnings.push("log: " + e); }
  var rowsDeleted = 0;
  try { rowsDeleted = deleteRowsById_("Jobs", JOB_HEADERS, "job_id", job.job_id); }
  catch (e) { throw new Error("Gagal menghapus baris Jobs: " + (e && e.message ? e.message : e)); }
  if (!rowsDeleted) throw new Error("Baris job tidak ditemukan saat menghapus (mungkin sudah dihapus).");

  log_("", user.email, "Job dihapus: " + job.brand + " (" + job.job_id + ")" +
    (warnings.length ? " · peringatan: " + warnings.join("; ") : ""));
  return {
    job_id: job.job_id, deleted: rowsDeleted, tab_deleted: tabDeleted,
    file_deleted: fileDeleted, logs_deleted: logsDeleted, warnings: warnings
  };
}

function rerunZero_(user, payload) {
  var job = getJob_(payload.job_id);
  if (!job) throw new Error("Job tidak ditemukan.");
  if (!canAccessJob_(user, job)) throw new Error("Akses ditolak.");
  var zeros = [];
  try { zeros = JSON.parse(job.zero_targets_json || "[]") || []; } catch (e) { zeros = []; }
  if (!zeros.length) throw new Error("Tidak ada target 0 hasil pada job ini.");
  var origSpec = {};
  try { origSpec = JSON.parse(job.spec_json || "{}"); } catch (e) { origSpec = {}; }
  return createJob_(user, {
    country: job.country,
    brand: job.brand,
    level: "",
    scope: {},
    mode: "unit",
    tile: 0,
    params: origSpec.params || {},
    explicit_admins: zeros
  });
}

function previewJob_(user, payload) {
  var job = getJob_(payload.job_id);
  if (!job) throw new Error("Job tidak ditemukan.");
  if (!canAccessJob_(user, job)) throw new Error("Akses ditolak.");
  var tabName = String(job.result_sheet || "");
  if (!tabName) throw new Error("Job ini tidak memiliki tab hasil.");
  var sh = sheet_().getSheetByName(tabName);
  if (!sh) throw new Error("Tab hasil tidak ada: " + tabName);
  var limit = Math.max(1, Math.min(500, Number(payload.limit || 10)));
  var values = sh.getDataRange().getValues();
  if (!values.length) return { headers: [], rows: [], total: 0 };
  var headers = values[0].map(function (h) { return fmtVal_(h); });
  var rows = values.slice(1, 1 + limit).map(function (r) {
    return headers.map(function (_, i) { return fmtVal_(r[i]); });
  });
  return { headers: headers, rows: rows, total: Math.max(0, values.length - 1) };
}

function jobLogs_(user, payload) {
  var jobId = String(payload.job_id || "");
  if (jobId) {
    var job = getJob_(jobId);
    if (!job) throw new Error("Job tidak ditemukan.");
    if (!canAccessJob_(user, job)) throw new Error("Akses ditolak.");
  }
  var limit = Math.max(1, Math.min(500, Number(payload.limit || 80)));
  var rows = readTable_("Logs", LOG_HEADERS);
  var admin = isAdmin_(user.email);
  var filtered = rows.filter(function (l) {
    if (jobId) return String(l.job_id) === jobId || String(l.job_id) === "";
    return admin || String(l.user).toLowerCase() === String(user.email).toLowerCase();
  });
  return filtered.slice(-limit).map(function (l) {
    return {
      timestamp: fmtVal_(l.timestamp),
      job_id: String(l.job_id || ""),
      message: String(l.message || "")
    };
  });
}

function jobStats_(user) {
  var jobs = readTable_("Jobs", JOB_HEADERS);
  var admin = isAdmin_(user.email);
  var mine = jobs.filter(function (j) {
    return admin || String(j.user).toLowerCase() === String(user.email).toLowerCase();
  });
  var stats = { total: mine.length, done: 0, running: 0, queued: 0, failed: 0, active: 0, records: 0 };
  mine.forEach(function (j) {
    var s = String(j.status || "").toLowerCase();
    if (s === "done") stats.done++;
    else if (s === "failed") stats.failed++;
    else if (s === "queued") stats.queued++;
    else if (s === "running" || s === "dispatched") stats.running++;
    if (s === "running" || s === "dispatched" || s === "queued") stats.active++;
    var rec = Number(j.records || 0);
    if (!rec) {
      var m = String(j.progress || "").match(/\((\d+)\s*records?\)/i);
      if (m) rec = Number(m[1]);
    }
    stats.records += rec;
  });
  return stats;
}

// ------------------------------------------------------------
// Cleaning data: filter otomatis + review manual
// Logika filter mengikuti "1. Cleaning.py" (prefix brand, blacklist nama,
// blacklist kategori). Baris aman langsung "keep"; baris berisiko "pending"
// sampai diputuskan manual agar tidak terhapus tanpa sengaja.
// ------------------------------------------------------------
function cleanTextCell_(value) {
  if (value === null || value === undefined) return "";
  return String(value).replace(/\u0000/g, "").trim();
}

function cleanSheetUrl_(tabName, gid) {
  if (!tabName) return "";
  var base = "https://docs.google.com/spreadsheets/d/" + prop_("SHEET_ID") + "/edit";
  if (gid === "" || gid === null || gid === undefined) return base;
  return base + "#gid=" + gid;
}

function cleanCreateTab_(ss, prefix, brand, cleanId) {
  var stamp = Utilities.formatDate(new Date(), "Asia/Jakarta", "yyyyMMdd_HHmmss");
  var safe = String(brand || "clean").replace(/[^A-Za-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 24) || "clean";
  var name = prefix + "_" + safe + "_" + stamp + "_" + String(cleanId).slice(0, 8);
  return ss.insertSheet(name);
}

function cleanParseCsv_(text) {
  text = String(text || "");
  if (text.charCodeAt(0) === 0xFEFF) text = text.slice(1);
  if (!text.trim()) throw new Error("CSV kosong.");
  var firstLine = text.split(/\r?\n/)[0] || "";
  var delimiters = [",", ";", "\t"];
  var delimiter = ",";
  var best = -1;
  delimiters.forEach(function (d) {
    var count = firstLine.split(d).length - 1;
    if (count > best) { best = count; delimiter = d; }
  });
  var values;
  try {
    values = Utilities.parseCsv(text, delimiter);
  } catch (e) {
    throw new Error("CSV tidak bisa dibaca: " + (e && e.message ? e.message : e));
  }
  if (!values || !values.length) throw new Error("CSV tidak berisi data.");
  var headers = values[0].map(function (h) {
    return String(h || "").replace(/^\uFEFF/, "").trim();
  });
  var rows = [];
  for (var i = 1; i < values.length; i++) {
    var row = values[i] || [];
    var hasData = row.some(function (cell) { return cleanTextCell_(cell) !== ""; });
    if (hasData) rows.push(row);
  }
  return { headers: headers, rows: rows };
}

function cleanColumnMap_(headers) {
  var map = {};
  (headers || []).forEach(function (h, i) {
    var key = String(h || "").trim().toLowerCase();
    if (key && map[key] === undefined) map[key] = i;
  });
  return map;
}

function cleanBrandTerms_(brand) {
  var seen = {};
  var out = [];
  String(brand || "").split(/[|;,\n]+/).forEach(function (term) {
    term = term.trim();
    if (!term) return;
    var key = term.toLowerCase();
    if (Object.prototype.hasOwnProperty.call(seen, key)) return;
    seen[key] = true;
    out.push(term);
  });
  return out;
}

function cleanBrandPattern_(brand) {
  var patterns = cleanBrandTerms_(brand).map(function (term) {
    var escaped = String(term).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    escaped = escaped.replace(/ /g, "[\\s-]*");
    return "^" + escaped + "\\b";
  });
  return new RegExp(patterns.length ? patterns.join("|") : "^$", "i");
}

function cleanMatchTerms_(value, terms) {
  var text = String(value || "").toLowerCase();
  var out = [];
  (terms || []).forEach(function (term) {
    if (text.indexOf(String(term).toLowerCase()) >= 0) out.push(term);
  });
  return out;
}

function cleanParseList_(value) {
  var raw = Array.isArray(value) ? value : String(value === null || value === undefined ? "" : value).split(/[\n,;]+/);
  var seen = {};
  var out = [];
  raw.forEach(function (term) {
    term = String(term || "").trim();
    if (!term) return;
    var key = term.toLowerCase();
    if (Object.prototype.hasOwnProperty.call(seen, key)) return;
    seen[key] = true;
    out.push(term);
  });
  return out;
}

function cleanAssess_(name, category, brand, brandPattern, blacklistNames, blacklistCategories) {
  var risk = [];
  var reasons = [];
  var prefixOk = brandPattern.test(String(name || ""));
  if (!prefixOk) {
    risk.push("prefix");
    reasons.push('nama tidak diawali "' + brand + '"');
  }
  var nameHits = cleanMatchTerms_(name, blacklistNames);
  if (nameHits.length) {
    risk.push("nama");
    reasons.push("nama memuat " + nameHits.map(function (t) { return '"' + t + '"'; }).join(", "));
  }
  var categoryHits = cleanMatchTerms_(category, blacklistCategories);
  if (categoryHits.length) {
    risk.push("kategori");
    reasons.push("kategori " + categoryHits.map(function (t) { return '"' + t + '"'; }).join(", "));
  }
  return { risk: risk, reason: reasons.join("; "), prefixOk: prefixOk };
}

function cleanAnalyzeMatrix_(matrix, headers, brand, blacklistNames, blacklistCategories) {
  blacklistNames = blacklistNames || [];
  blacklistCategories = blacklistCategories || [];
  var map = cleanColumnMap_(headers);
  var missing = [];
  CLEAN_OUTPUT_COLUMNS.forEach(function (col) {
    if (map[String(col).toLowerCase()] === undefined) missing.push(col);
  });
  if (missing.length) throw new Error("Kolom tidak ditemukan: " + missing.join(", "));
  var idx = {
    name: map["name_geocode"],
    address: map["address_geocode"],
    longitude: map["longitude_geocode"],
    latitude: map["latitude_geocode"],
    category: map["category_geocode"],
    url: map["source_url"]
  };
  var brandPattern = cleanBrandPattern_(brand);
  var rows = [];
  var safe = 0;
  var risky = 0;
  var removed = 0;
  var pending = 0;
  (matrix || []).forEach(function (row) {
    var name = cleanTextCell_(row[idx.name]);
    var address = cleanTextCell_(row[idx.address]);
    var longitude = cleanTextCell_(row[idx.longitude]);
    var latitude = cleanTextCell_(row[idx.latitude]);
    var category = cleanTextCell_(row[idx.category]);
    var url = idx.url === undefined ? "" : cleanTextCell_(row[idx.url]);
    if (!name && !address && !longitude && !latitude && !category) return;
    var assessed = cleanAssess_(name, category, brand, brandPattern, blacklistNames, blacklistCategories);
    var decision = "keep";
    if (!assessed.prefixOk) {
      decision = "remove";
      removed++;
    } else if (assessed.risk.length) {
      decision = "pending";
      pending++;
    } else {
      safe++;
    }
    if (assessed.risk.length) risky++;
    rows.push([
      rows.length + 1, decision, assessed.risk.join(","), assessed.reason,
      name, address, longitude, latitude, category, url
    ]);
  });
  return { rows: rows, total: rows.length, safe: safe, risky: risky, removed: removed, pending: pending };
}

function cleanWriteRows_(sh, rows, header) {
  var all = [];
  if (header) all.push(header.slice());
  (rows || []).forEach(function (r) { all.push(r.slice()); });
  if (!all.length) return;
  var cols = 0;
  all.forEach(function (r) { if (r.length > cols) cols = r.length; });
  all.forEach(function (r) { while (r.length < cols) r.push(""); });
  if (sh.getMaxRows() < all.length) {
    sh.insertRowsAfter(sh.getMaxRows(), all.length - sh.getMaxRows());
  }
  if (sh.getMaxColumns() < cols) {
    sh.insertColumnsAfter(sh.getMaxColumns(), cols - sh.getMaxColumns());
  }
  var chunkSize = 1500;
  for (var start = 0; start < all.length; start += chunkSize) {
    var part = all.slice(start, start + chunkSize);
    var range = sh.getRange(start + 1, 1, part.length, cols);
    range.setNumberFormat("@");
    range.setValues(part);
  }
}

function cleaningDefaults_() {
  return {
    blacklist_names: CLEAN_BLACKLIST_NAMES.slice(),
    blacklist_categories: CLEAN_BLACKLIST_CATEGORIES.slice()
  };
}

function startCleaning_(user, payload) {
  var brand = String(payload.brand || "").trim();
  if (!brand) throw new Error("Nama kompetitor/brand wajib diisi.");
  var brandTerms = cleanBrandTerms_(brand);
  if (!brandTerms.length) throw new Error("Nama kompetitor/brand wajib diisi.");
  brand = brandTerms.join(" | ");
  var sourceType = String(payload.source_type || "job").toLowerCase();
  if (sourceType !== "job" && sourceType !== "csv") throw new Error("Sumber data tidak dikenal.");
  var blacklistNames = payload.blacklist_names === undefined
    ? CLEAN_BLACKLIST_NAMES.slice()
    : cleanParseList_(payload.blacklist_names);
  var blacklistCategories = payload.blacklist_categories === undefined
    ? CLEAN_BLACKLIST_CATEGORIES.slice()
    : cleanParseList_(payload.blacklist_categories);

  var matrix = [];
  var headers = [];
  var sourceName = "";
  var sourceJob = "";

  if (sourceType === "job") {
    var job = getJob_(payload.job_id);
    if (!job) throw new Error("Job tidak ditemukan.");
    if (!canAccessJob_(user, job)) throw new Error("Akses ditolak.");
    if (String(job.status || "").toLowerCase() !== "done") {
      throw new Error("Job harus berstatus done sebelum dibersihkan.");
    }
    var tabName = String(job.result_sheet || "");
    if (!tabName) throw new Error("Job ini tidak memiliki tab hasil.");
    var sourceSheet = sheet_().getSheetByName(tabName);
    if (!sourceSheet) throw new Error("Tab hasil tidak ada: " + tabName);
    var values = sourceSheet.getDataRange().getValues();
    if (!values.length) throw new Error("Tab hasil kosong.");
    headers = values[0].map(function (h) { return fmtVal_(h); });
    matrix = values.slice(1);
    sourceName = tabName;
    sourceJob = String(job.job_id || "");
  } else {
    var text = String(payload.csv_text || "");
    if (!text) throw new Error("File CSV belum dipilih.");
    if (text.length > 25 * 1024 * 1024) throw new Error("Ukuran CSV terlalu besar (maksimal ±25 MB).");
    var parsed = cleanParseCsv_(text);
    headers = parsed.headers;
    matrix = parsed.rows;
    sourceName = String(payload.filename || "upload.csv").slice(0, 160);
  }

  var analyzed = cleanAnalyzeMatrix_(matrix, headers, brand, blacklistNames, blacklistCategories);
  if (!analyzed.total) throw new Error("Tidak ada baris data yang bisa dibersihkan.");

  var cleanId = Utilities.getUuid();
  var ss = sheet_();
  var reviewSheet = cleanCreateTab_(ss, "Clean", brand, cleanId);
  cleanWriteRows_(reviewSheet, analyzed.rows, CLEAN_REVIEW_HEADERS);
  reviewSheet.setFrozenRows(1);

  var created = nowStr_();
  tab_("Cleanings", CLEAN_HEADERS).appendRow([
    cleanId, user.email, brand, sourceType, sourceName, sourceJob,
    "review", analyzed.total, analyzed.safe, analyzed.risky,
    analyzed.safe, analyzed.removed, analyzed.pending,
    reviewSheet.getName(), reviewSheet.getSheetId(), "", "",
    created, "", ""
  ]);
  log_(cleanId, user.email, "Cleaning dibuat: " + brand + " · " + analyzed.total + " baris (" +
    analyzed.safe + " aman, " + analyzed.removed + " langsung dihapus, " +
    analyzed.pending + " perlu review)");

  return {
    clean_id: cleanId,
    brand: brand,
    tab: reviewSheet.getName(),
    tab_url: cleanSheetUrl_(reviewSheet.getName(), reviewSheet.getSheetId()),
    total: analyzed.total,
    safe: analyzed.safe,
    risky: analyzed.risky,
    removed: analyzed.removed,
    pending: analyzed.pending
  };
}

function findCleaning_(cleanId) {
  var sh = tab_("Cleanings", CLEAN_HEADERS);
  var values = sh.getDataRange().getValues();
  if (values.length < 2) return null;
  var head = values[0].map(function (h) { return String(h).trim(); });
  var col = {};
  head.forEach(function (h, i) { if (col[h] === undefined) col[h] = i; });
  var idCol = col.clean_id === undefined ? 0 : col.clean_id;
  for (var r = 1; r < values.length; r++) {
    if (String(values[r][idCol] || "") !== String(cleanId)) continue;
    var obj = {};
    CLEAN_HEADERS.forEach(function (h) {
      var ci = col[h] === undefined ? -1 : col[h];
      obj[h] = ci >= 0 ? values[r][ci] : "";
    });
    return { rowIndex: r + 1, obj: obj, col: col };
  }
  return null;
}

function cleanPublic_(obj) {
  return {
    clean_id: String(obj.clean_id || ""),
    user: String(obj.user || ""),
    brand: String(obj.brand || ""),
    source_type: String(obj.source_type || ""),
    source_name: String(obj.source_name || ""),
    source_job: String(obj.source_job || ""),
    status: String(obj.status || "review").toLowerCase(),
    total: Number(obj.total || 0),
    safe: Number(obj.safe || 0),
    risky: Number(obj.risky || 0),
    kept: Number(obj.kept || 0),
    removed: Number(obj.removed || 0),
    pending: Number(obj.pending || 0),
    tab: String(obj.tab || ""),
    final_tab: String(obj.final_tab || ""),
    tab_url: cleanSheetUrl_(obj.tab, obj.tab_gid),
    final_url: cleanSheetUrl_(obj.final_tab, obj.final_gid),
    csv_url: String(obj.csv_url || ""),
    created_at: fmtVal_(obj.created_at),
    finished_at: fmtVal_(obj.finished_at)
  };
}

function canAccessClean_(user, clean) {
  if (!clean) return false;
  return isAdmin_(user.email) || String(clean.user).toLowerCase() === String(user.email).toLowerCase();
}

function listCleanings_(user) {
  var all = readTable_("Cleanings", CLEAN_HEADERS);
  var admin = isAdmin_(user.email);
  var out = all.filter(function (c) {
    return admin || String(c.user).toLowerCase() === String(user.email).toLowerCase();
  });
  out.sort(function (a, b) { return String(b.created_at).localeCompare(String(a.created_at)); });
  return out.slice(0, 100).map(cleanPublic_);
}

function cleanRowObject_(row) {
  var decision = String(row[1] || "").toLowerCase();
  return {
    row_id: Number(row[0] || 0),
    decision: decision || "pending",
    risk: String(row[2] || ""),
    reason: String(row[3] || ""),
    name: String(row[4] || ""),
    address: String(row[5] || ""),
    longitude: String(row[6] || ""),
    latitude: String(row[7] || ""),
    category: String(row[8] || ""),
    source_url: String(row[9] || "")
  };
}

function cleaningGet_(user, payload) {
  var clean = findCleaning_(payload.clean_id);
  if (!clean) throw new Error("Sesi cleaning tidak ditemukan.");
  if (!canAccessClean_(user, clean.obj)) throw new Error("Akses ditolak.");
  var filter = String(payload.filter || "all").toLowerCase();
  if (["all", "risky", "pending", "keep", "remove"].indexOf(filter) < 0) filter = "all";
  var search = String(payload.q || "").trim().toLowerCase();
  var offset = Math.max(0, Number(payload.offset || 0));
  var limit = Math.max(1, Math.min(200, Number(payload.limit || 100)));
  var rows = [];
  var filtered = 0;
  var tabName = String(clean.obj.tab || "");

  if (tabName) {
    var sh = sheet_().getSheetByName(tabName);
    if (!sh) throw new Error("Tab review tidak ditemukan: " + tabName);
    var values = sh.getDataRange().getValues();
    for (var i = 1; i < values.length; i++) {
      var r = values[i] || [];
      var decision = String(r[1] || "").toLowerCase() || "pending";
      var risk = String(r[2] || "");
      if (filter === "risky" && !risk) continue;
      if (filter === "pending" && decision !== "pending") continue;
      if (filter === "keep" && decision !== "keep") continue;
      if (filter === "remove" && decision !== "remove") continue;
      if (search) {
        var haystack = (String(r[4] || "") + " " + String(r[5] || "") + " " + String(r[8] || "")).toLowerCase();
        if (haystack.indexOf(search) < 0) continue;
      }
      filtered++;
      if (filtered > offset && rows.length < limit) rows.push(cleanRowObject_(r));
    }
  }

  return {
    clean: cleanPublic_(clean.obj),
    rows: rows,
    filtered: filtered,
    offset: offset,
    limit: limit
  };
}

function updateCleanFields_(clean, fields) {
  var sh = tab_("Cleanings", CLEAN_HEADERS);
  Object.keys(fields).forEach(function (key) {
    var ci = clean.col && clean.col[key] !== undefined ? clean.col[key] : CLEAN_HEADERS.indexOf(key);
    if (ci >= 0) sh.getRange(clean.rowIndex, Number(ci) + 1).setValue(fields[key]);
  });
}

function cleaningDecide_(user, payload) {
  var clean = findCleaning_(payload.clean_id);
  if (!clean) throw new Error("Sesi cleaning tidak ditemukan.");
  if (!canAccessClean_(user, clean.obj)) throw new Error("Akses ditolak.");
  if (String(clean.obj.status || "").toLowerCase() === "done") {
    throw new Error("Cleaning sudah difinalisasi; keputusan tidak bisa diubah.");
  }
  var total = Number(clean.obj.total || 0);
  if (!total) throw new Error("Sesi cleaning tidak memiliki baris.");
  var decisions = payload.decisions;
  if (!decisions) decisions = [{ row_id: payload.row_id, decision: payload.decision }];
  if (!decisions.length) throw new Error("Tidak ada keputusan yang dikirim.");
  if (decisions.length > 500) decisions = decisions.slice(0, 500);

  var tabName = String(clean.obj.tab || "");
  var sh = sheet_().getSheetByName(tabName);
  if (!sh) throw new Error("Tab review tidak ditemukan: " + tabName);
  var decisionValues = sh.getRange(2, 2, total, 1).getValues();
  var allowed = { keep: true, remove: true, pending: true };
  decisions.forEach(function (d) {
    var idx = Number(d.row_id) - 1;
    var dec = String(d.decision || "").toLowerCase();
    if (idx < 0 || idx >= total || !allowed[dec]) return;
    decisionValues[idx][0] = dec;
  });
  sh.getRange(2, 2, total, 1).setValues(decisionValues);

  var kept = 0;
  var removed = 0;
  var pending = 0;
  decisionValues.forEach(function (cell) {
    var value = String(cell[0] || "").toLowerCase();
    if (value === "keep") kept++;
    else if (value === "remove") removed++;
    else pending++;
  });
  updateCleanFields_(clean, { kept: kept, removed: removed, pending: pending });
  return { kept: kept, removed: removed, pending: pending };
}

function cleaningFinish_(user, payload) {
  var clean = findCleaning_(payload.clean_id);
  if (!clean) throw new Error("Sesi cleaning tidak ditemukan.");
  if (!canAccessClean_(user, clean.obj)) throw new Error("Akses ditolak.");
  if (String(clean.obj.status || "").toLowerCase() === "done") {
    return Object.assign(cleanPublic_(clean.obj), { warnings: [] });
  }

  var total = Number(clean.obj.total || 0);
  var tabName = String(clean.obj.tab || "");
  var sh = sheet_().getSheetByName(tabName);
  if (!sh) throw new Error("Tab review tidak ditemukan: " + tabName);
  var values = sh.getDataRange().getValues();
  var keptRows = [];
  var removed = 0;
  var pending = 0;
  for (var i = 1; i < values.length; i++) {
    var r = values[i] || [];
    var decision = String(r[1] || "").toLowerCase() || "pending";
    if (decision === "keep") {
      keptRows.push([r[4], r[5], r[6], r[7], r[8]].map(cleanTextCell_));
    } else if (decision === "remove") {
      removed++;
    } else {
      pending++;
    }
  }
  if (pending > 0 && !payload.confirm_pending) {
    throw new Error("Masih ada " + pending + " baris berisiko yang belum diputuskan. " +
      "Konfirmasi finalisasi untuk mengecualikannya.");
  }
  if (pending > 0) removed += pending;

  var finalSheet = cleanCreateTab_(sheet_(), "CleanResult", clean.obj.brand, clean.obj.clean_id);
  cleanWriteRows_(finalSheet, keptRows, CLEAN_OUTPUT_COLUMNS);
  finalSheet.setFrozenRows(1);

  var warnings = [];
  var csvUrl = "";
  var folderId = prop_("DRIVE_FOLDER_ID");
  if (folderId) {
    try {
      var exported = exportSheetToDrive_(
        finalSheet.getName(), String(clean.obj.brand || "clean") + "_clean",
        String(clean.obj.clean_id || ""), folderId, user.email
      );
      csvUrl = exported.url || "";
    } catch (e) {
      warnings.push("CSV Drive gagal: " + (e && e.message ? e.message : e));
    }
  } else {
    warnings.push("DRIVE_FOLDER_ID belum diset; CSV Drive tidak dibuat. Tab hasil tetap bisa diunduh.");
  }

  var finished = nowStr_();
  updateCleanFields_(clean, {
    status: "done",
    kept: keptRows.length,
    removed: removed,
    pending: 0,
    final_tab: finalSheet.getName(),
    final_gid: finalSheet.getSheetId(),
    finished_at: finished,
    csv_url: csvUrl
  });
  clean.obj.status = "done";
  clean.obj.kept = keptRows.length;
  clean.obj.removed = removed;
  clean.obj.pending = 0;
  clean.obj.final_tab = finalSheet.getName();
  clean.obj.final_gid = finalSheet.getSheetId();
  clean.obj.finished_at = finished;
  clean.obj.csv_url = csvUrl;
  log_(clean.obj.clean_id, user.email, "Cleaning selesai: " + clean.obj.brand + " · " +
    keptRows.length + " disimpan, " + removed + " dihapus");
  return Object.assign(cleanPublic_(clean.obj), { warnings: warnings });
}

function deleteCleanTab_(tabName) {
  tabName = String(tabName || "");
  if (!tabName) return false;
  if (["Users", "Jobs", "Logs", "Cleanings"].indexOf(tabName) >= 0) return false;
  if (!/^(Clean|CleanResult)_/.test(tabName)) return false;
  var ss = sheet_();
  var sh = ss.getSheetByName(tabName);
  if (!sh) return false;
  ss.deleteSheet(sh);
  return true;
}

function cleaningDelete_(user, payload) {
  var clean = findCleaning_(payload.clean_id);
  if (!clean) throw new Error("Sesi cleaning tidak ditemukan.");
  if (!canAccessClean_(user, clean.obj)) throw new Error("Akses ditolak.");
  var warnings = [];
  var reviewDeleted = false;
  var finalDeleted = false;
  try { reviewDeleted = deleteCleanTab_(clean.obj.tab); }
  catch (e) { warnings.push("tab review: " + (e && e.message ? e.message : e)); }
  try { finalDeleted = deleteCleanTab_(clean.obj.final_tab); }
  catch (e) { warnings.push("tab hasil: " + (e && e.message ? e.message : e)); }
  var fileDeleted = false;
  var fileId = driveIdFromUrl_(clean.obj.csv_url);
  if (fileId) {
    try { DriveApp.getFileById(fileId).setTrashed(true); fileDeleted = true; }
    catch (e) { warnings.push("file CSV: " + (e && e.message ? e.message : e)); }
  }
  try { deleteRowsById_("Logs", LOG_HEADERS, "job_id", clean.obj.clean_id); }
  catch (e) { warnings.push("log: " + (e && e.message ? e.message : e)); }
  var deleted = 0;
  try { deleted = deleteRowsById_("Cleanings", CLEAN_HEADERS, "clean_id", clean.obj.clean_id); }
  catch (e) { throw new Error("Gagal menghapus baris Cleanings: " + (e && e.message ? e.message : e)); }
  if (!deleted) throw new Error("Baris cleaning tidak ditemukan (mungkin sudah dihapus).");
  log_("", user.email, "Cleaning dihapus: " + clean.obj.brand + " (" + clean.obj.clean_id + ")" +
    (warnings.length ? " · peringatan: " + warnings.join("; ") : ""));
  return {
    clean_id: clean.obj.clean_id,
    review_deleted: reviewDeleted,
    final_deleted: finalDeleted,
    file_deleted: fileDeleted,
    warnings: warnings
  };
}

// ------------------------------------------------------------
// Dispatch (hingga MAX_PARALLEL job berjalan bersamaan)
// ------------------------------------------------------------
function dispatch() {
  var lock = LockService.getScriptLock();
  if (!lock.tryLock(20000)) return;
  try {
    var max = Number(prop_("MAX_PARALLEL", "3"));
    if (!max || max < 1) max = 1;

    var jobs = readTable_("Jobs", JOB_HEADERS);
    var active = jobs.filter(function (j) {
      var s = String(j.status).toLowerCase();
      return s === "running" || s === "dispatched";
    }).length;
    if (active >= max) return;

    var queued = jobs.filter(function (j) { return String(j.status).toLowerCase() === "queued"; });
    queued.sort(function (a, b) { return String(a.created_at).localeCompare(String(b.created_at)); });

    var slots = max - active;
    for (var i = 0; i < queued.length && i < slots; i++) {
      var job = queued[i];
      try {
        var spec = JSON.parse(job.spec_json);
        triggerWorkflow_(spec, job.job_id);
        setJobField_(job.job_id, "status", "dispatched");
      } catch (e) {
        log_(job.job_id, job.user, "dispatch gagal: " + e);
      }
    }
  } finally {
    lock.releaseLock();
  }
}

function setJobField_(jobId, field, value) {
  var sh = tab_("Jobs", JOB_HEADERS);
  var values = sh.getDataRange().getValues();
  var col = JOB_HEADERS.indexOf(field);
  if (col < 0) return;
  for (var i = 1; i < values.length; i++) {
    if (String(values[i][0]) === String(jobId)) {
      sh.getRange(i + 1, col + 1).setValue(value);
      return;
    }
  }
}

function triggerWorkflow_(spec, jobId) {
  var owner = prop_("GITHUB_OWNER");
  var repo = prop_("GITHUB_REPO");
  var pat = prop_("GITHUB_PAT");
  var ref = prop_("GITHUB_REF", "main");
  if (!owner || !repo || !pat) throw new Error("Konfigurasi GitHub belum lengkap.");
  var url = "https://api.github.com/repos/" + owner + "/" + repo +
    "/actions/workflows/scrape.yml/dispatches";
  var res = UrlFetchApp.fetch(url, {
    method: "post",
    contentType: "application/json",
    headers: {
      Authorization: "Bearer " + pat,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28"
    },
    payload: JSON.stringify({ ref: ref, inputs: { job_id: jobId, spec_json: JSON.stringify(spec) } }),
    muteHttpExceptions: true
  });
  if (res.getResponseCode() >= 300) {
    throw new Error("GitHub dispatch gagal (" + res.getResponseCode() + "): " + res.getContentText());
  }
}

function log_(jobId, user, msg) {
  try {
    tab_("Logs", LOG_HEADERS).appendRow([nowStr_(), String(jobId || ""), String(user || ""), String(msg || "")]);
  } catch (e) {}
}

// ------------------------------------------------------------
// Admin
// ------------------------------------------------------------
function isAdmin_(email) {
  var list = prop_("ADMIN_EMAILS").toLowerCase().split(",").map(function (s) { return s.trim(); });
  return list.indexOf(String(email || "").toLowerCase()) >= 0;
}

function adminCreateKey_(email, quota) {
  email = String(email || "").trim().toLowerCase();
  if (!email) throw new Error("Email wajib.");
  var key = "poi_" + Utilities.getUuid().replace(/-/g, "");
  var user = findUserByEmail_(email);
  if (user) {
    var sh = tab_("Users", USER_HEADERS);
    var values = sh.getDataRange().getValues();
    for (var i = 1; i < values.length; i++) {
      if (String(values[i][0]).toLowerCase() === email) {
        sh.getRange(i + 1, 2).setValue(hashKey_(key));
        sh.getRange(i + 1, 3).setValue(Number(quota || 0));
        sh.getRange(i + 1, 5).setValue(true);
        sh.getRange(i + 1, 7).setValue(key);
        break;
      }
    }
  } else {
    appendUser_(email, hashKey_(key), Number(quota || 0), true, key);
  }
  shareSheetViewer_(email);
  return { email: email, api_key: key };
}

function adminGetKey_(email) {
  var user = findUserByEmail_(email);
  if (!user) throw new Error("User tidak ditemukan: " + email);
  if (!user.api_key) throw new Error("Key user ini tersimpan sebagai hash saja (dibuat sebelum fitur ini). Jalankan reset key sekali.");
  return { email: user.email, api_key: user.api_key, quota: Number(user.quota || 0), used: Number(user.used || 0), active: user.active };
}

function adminSetQuota_(email, quota) {
  var sh = tab_("Users", USER_HEADERS);
  var values = sh.getDataRange().getValues();
  for (var i = 1; i < values.length; i++) {
    if (String(values[i][0]).toLowerCase() === String(email).toLowerCase()) {
      sh.getRange(i + 1, 3).setValue(Number(quota || 0));
      return { email: email, quota: Number(quota || 0) };
    }
  }
  throw new Error("User tidak ditemukan: " + email);
}

function shareSheetViewer_(email) {
  try {
    var ss = sheet_();
    DriveApp.getFileById(ss.getId()).addViewer(String(email).trim().toLowerCase());
  } catch (e) {
    log_("", email, "share viewer gagal: " + e);
  }
}

function adminSetActive_(email, active) {
  var sh = tab_("Users", USER_HEADERS);
  var values = sh.getDataRange().getValues();
  for (var i = 1; i < values.length; i++) {
    if (String(values[i][0]).toLowerCase() === String(email).toLowerCase()) {
      sh.getRange(i + 1, 5).setValue(!!active);
      return true;
    }
  }
  return false;
}

function adminListUsers_() {
  return readTable_("Users", USER_HEADERS).map(function (u) {
    return {
      email: u.email, quota: u.quota, used: u.used, active: u.active,
      created_at: fmtVal_(u.created_at), has_key: !!u.api_key
    };
  });
}

// ------------------------------------------------------------
// Web app + API
// ------------------------------------------------------------
function doGet() {
  return HtmlService.createHtmlOutputFromFile("Index")
    .setTitle("POI Scraper Studio")
    .setXFrameOptionsMode(HtmlService.XFrameOptionsMode.ALLOWALL)
    .addMetaTag("viewport", "width=device-width, initial-scale=1, maximum-scale=5");
}

function doPost(e) {
  var out;
  try {
    var body = JSON.parse((e && e.postData && e.postData.contents) || "{}");
    out = handleAction_(body.action, body.payload || {}, {
      apiKey: body.api_key || "",
      idToken: body.id_token || ""
    }, false);
  } catch (err) {
    out = { ok: false, error: String(err && err.message ? err.message : err) };
  }
  return ContentService.createTextOutput(JSON.stringify(out)).setMimeType(ContentService.MimeType.JSON);
}

function handleAction_(action, payload, ident, isUi) {
  try {
    if (action === "options") {
      return { ok: true, data: { index_base: prop_("INDEX_BASE") } };
    }
    if (action === "config") {
      return { ok: true, data: {
        app_version: APP_VERSION,
        index_base: prop_("INDEX_BASE"),
        oauth_client_id: prop_("OAUTH_CLIENT_ID"),
        is_admin: ident && ident.idToken ? null : false
      } };
    }

    var user = resolveIdentity_(ident);

    if (action === "me") {
      return { ok: true, data: {
        email: user.email, quota: user.quota, used: user.used, is_admin: isAdmin_(user.email)
      } };
    }
    if (action === "jobs.create") {
      return { ok: true, data: createJob_(user, payload) };
    }
    if (action === "jobs.get") {
      var job = getJob_(payload.job_id);
      if (!job) throw new Error("Job tidak ditemukan.");
      if (!isAdmin_(user.email) && String(job.user).toLowerCase() !== String(user.email).toLowerCase()) {
        throw new Error("Akses ditolak.");
      }
      return { ok: true, data: job };
    }
    if (action === "jobs.list") {
      return { ok: true, data: listJobs_(user) };
    }
    if (action === "jobs.preview") {
      return { ok: true, data: previewJob_(user, payload) };
    }
    if (action === "jobs.logs") {
      return { ok: true, data: jobLogs_(user, payload) };
    }
    if (action === "jobs.stats") {
      return { ok: true, data: jobStats_(user) };
    }
    if (action === "jobs.pause") {
      return { ok: true, data: jobControl_(user, payload, "pause") };
    }
    if (action === "jobs.resume") {
      return { ok: true, data: jobControl_(user, payload, "resume") };
    }
    if (action === "jobs.cancel") {
      return { ok: true, data: jobControl_(user, payload, "cancel") };
    }
    if (action === "jobs.delete") {
      return { ok: true, data: deleteJob_(user, payload) };
    }
    if (action === "jobs.rerunZero") {
      return { ok: true, data: rerunZero_(user, payload) };
    }
    if (action === "cleaning.defaults") {
      return { ok: true, data: cleaningDefaults_() };
    }
    if (action === "cleaning.start") {
      return { ok: true, data: startCleaning_(user, payload) };
    }
    if (action === "cleaning.list") {
      return { ok: true, data: listCleanings_(user) };
    }
    if (action === "cleaning.get") {
      return { ok: true, data: cleaningGet_(user, payload) };
    }
    if (action === "cleaning.decide") {
      return { ok: true, data: cleaningDecide_(user, payload) };
    }
    if (action === "cleaning.finish") {
      return { ok: true, data: cleaningFinish_(user, payload) };
    }
    if (action === "cleaning.delete") {
      return { ok: true, data: cleaningDelete_(user, payload) };
    }
    if (action === "admin.users") {
      if (!isAdmin_(user.email)) throw new Error("Hanya admin.");
      return { ok: true, data: adminListUsers_() };
    }
    if (action === "admin.createKey") {
      if (!isAdmin_(user.email)) throw new Error("Hanya admin.");
      return { ok: true, data: adminCreateKey_(payload.email, payload.quota) };
    }
    if (action === "admin.getKey") {
      if (!isAdmin_(user.email)) throw new Error("Hanya admin.");
      return { ok: true, data: adminGetKey_(payload.email) };
    }
    if (action === "admin.setQuota") {
      if (!isAdmin_(user.email)) throw new Error("Hanya admin.");
      return { ok: true, data: adminSetQuota_(payload.email, payload.quota) };
    }
    if (action === "admin.setActive") {
      if (!isAdmin_(user.email)) throw new Error("Hanya admin.");
      return { ok: true, data: adminSetActive_(payload.email, payload.active) };
    }
    if (action === "admin.diag") {
      if (!isAdmin_(user.email)) throw new Error("Hanya admin.");
      return { ok: true, data: diag_() };
    }
    throw new Error("Action tidak dikenal: " + action);
  } catch (err) {
    var msg = err && err.message ? err.message : String(err);
    if (err && err.stack) {
      msg += " | " + String(err.stack).split("\n").slice(0, 2).join(" ").trim();
    }
    return { ok: false, error: msg };
  }
}

// Wrapper untuk google.script.run dari dashboard
function uiCall(action, payload, ident) {
  return handleAction_(action, payload || {}, ident || {}, true);
}

// Endpoint khusus daftar job (fallback bila routing action bermasalah).
function uiJobsList(ident) {
  try {
    var user = resolveIdentity_(ident || {});
    return { ok: true, data: listJobs_(user), app_version: APP_VERSION };
  } catch (e) {
    return { ok: false, error: "uiJobsList: " + String(e && e.message ? e.message : e) };
  }
}

// Diagnostik cepat dari editor: uji listJobs_ tanpa lewat dashboard.
function testListJobs() {
  var users = readTable_("Users", USER_HEADERS);
  var email = users.length ? users[0].email : String(prop_("ADMIN_EMAILS", "")).split(",")[0];
  var out = listJobs_({ email: email });
  Logger.log("APP_VERSION=" + APP_VERSION);
  Logger.log("jobs for " + email + ": " + out.length);
  Logger.log(JSON.stringify(out).slice(0, 1500));
  return out;
}

// ------------------------------------------------------------
// Setup (jalankan sekali dari editor)
// ------------------------------------------------------------
function syncHeaders_(name, headers) {
  var sh = tab_(name, headers);
  sh.getRange(1, 1, 1, headers.length).setValues([headers]);
}

function setup() {
  tab_("Users", USER_HEADERS);
  tab_("Jobs", JOB_HEADERS);
  tab_("Logs", LOG_HEADERS);
  tab_("Cleanings", CLEAN_HEADERS);
  syncHeaders_("Users", USER_HEADERS);
  syncHeaders_("Jobs", JOB_HEADERS);
  syncHeaders_("Logs", LOG_HEADERS);
  syncHeaders_("Cleanings", CLEAN_HEADERS);
  var p = PropertiesService.getScriptProperties();
  var defaults = {
    GITHUB_OWNER: "arnarendito34",
    GITHUB_REPO: "poi-scraper",
    GITHUB_REF: "main",
    INDEX_BASE: "https://raw.githubusercontent.com/arnarendito34/poi-scraper/main/admin",
    ADMIN_EMAILS: "renditobloggerspot@gmail.com",
    DEFAULT_QUOTA: "0",
    MAX_PARALLEL: "3"
  };
  Object.keys(defaults).forEach(function (k) {
    if (!p.getProperty(k)) p.setProperty(k, defaults[k]);
  });
  Logger.log("Setup selesai. Sheet ID terdeteksi: " + (prop_("SHEET_ID") || "(belum diset)"));
}

function setupTriggers() {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    var fn = t.getHandlerFunction();
    if (fn === "dispatch" || fn === "exportCompleted") ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger("dispatch").timeBased().everyMinutes(1).create();
  ScriptApp.newTrigger("exportCompleted").timeBased().everyMinutes(5).create();
  Logger.log("Trigger dispatch (1 mnt) & exportCompleted (5 mnt) dipasang.");
}

// ------------------------------------------------------------
// Ekspor hasil ke CSV di Drive memakai AKUN PEMILIK (kuota sendiri).
// Service account tidak punya kuota Drive pribadi, jadi upload dari worker
// sering gagal. Fungsi ini menyusul setelah job selesai.
// ------------------------------------------------------------
function exportCompleted() {
  var folderId = prop_("DRIVE_FOLDER_ID");
  if (!folderId) {
    Logger.log("exportCompleted: DRIVE_FOLDER_ID belum diset. Lewati.");
    return 0;
  }
  var sh = tab_("Jobs", JOB_HEADERS);
  var values = sh.getDataRange().getValues();
  if (values.length < 2) return 0;
  var head = values[0].map(function (h) { return String(h); });
  var col = {};
  head.forEach(function (h, i) { col[h] = i; });

  var exported = 0;
  for (var r = 1; r < values.length; r++) {
    var row = values[r];
    var status = String(row[col.status] || "").toLowerCase();
    var outUrl = String(row[col.output_url] || "");
    var resultSheet = String(row[col.result_sheet] || "");
    if (status !== "done" || outUrl || !resultSheet) continue;
    try {
      var res = exportSheetToDrive_(resultSheet, row[col.brand], row[col.job_id], folderId, row[col.user]);
      sh.getRange(r + 1, col.output_url + 1).setValue(res.url);
      if (col.output_file_id !== undefined) sh.getRange(r + 1, col.output_file_id + 1).setValue(res.id);
      log_(row[col.job_id], row[col.user], "CSV diekspor ke Drive: " + res.url);
      exported++;
    } catch (e) {
      log_(row[col.job_id], row[col.user], "export CSV gagal: " + e);
    }
  }
  Logger.log("exportCompleted: " + exported + " job diekspor.");
  return exported;
}

function exportJobCsv(jobId) {
  var sh = tab_("Jobs", JOB_HEADERS);
  var values = sh.getDataRange().getValues();
  var head = values[0].map(function (h) { return String(h); });
  var col = {};
  head.forEach(function (h, i) { col[h] = i; });
  var folderId = prop_("DRIVE_FOLDER_ID");
  if (!folderId) throw new Error("DRIVE_FOLDER_ID belum diset.");
  for (var r = 1; r < values.length; r++) {
    if (String(values[r][col.job_id]) !== String(jobId)) continue;
    var res = exportSheetToDrive_(values[r][col.result_sheet], values[r][col.brand], jobId, folderId, values[r][col.user]);
    sh.getRange(r + 1, col.output_url + 1).setValue(res.url);
    if (col.output_file_id !== undefined) sh.getRange(r + 1, col.output_file_id + 1).setValue(res.id);
    Logger.log("CSV: " + res.url);
    return res;
  }
  throw new Error("Job tidak ditemukan: " + jobId);
}

function exportSheetToDrive_(tabName, brand, jobId, folderId, userEmail) {
  var ss = sheet_();
  var sh = ss.getSheetByName(tabName);
  if (!sh) throw new Error("Tab hasil tidak ada: " + tabName);
  var values = sh.getDataRange().getValues();
  if (!values.length) values = [[""]];

  var lines = values.map(function (row) {
    return row.map(function (cell) {
      var s;
      if (cell instanceof Date) {
        s = Utilities.formatDate(cell, "Asia/Jakarta", "yyyy-MM-dd HH:mm:ss");
      } else {
        s = cell === null || cell === undefined ? "" : String(cell);
      }
      if (s.indexOf('"') >= 0) s = s.replace(/"/g, '""');
      if (s.indexOf(",") >= 0 || s.indexOf('"') >= 0 || s.indexOf("\n") >= 0) s = '"' + s + '"';
      return s;
    }).join(",");
  });
  var csv = "\ufeff" + lines.join("\r\n");

  var safe = String(brand || "result").replace(/[^A-Za-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 24) || "result";
  var name = "Output_" + safe + "_" + String(jobId).slice(0, 8) + ".csv";
  var blob = Utilities.newBlob(csv, "text/csv", name);
  var file = DriveApp.getFolderById(folderId).createFile(blob);
  if (userEmail) {
    try { file.addViewer(String(userEmail).trim().toLowerCase()); } catch (e) {}
  }
  return { id: file.getId(), url: file.getUrl() };
}

function setSheetId(id) {
  PropertiesService.getScriptProperties().setProperty("SHEET_ID", String(id).trim());
  Logger.log("SHEET_ID diset.");
}

function setDriveFolder(id) {
  PropertiesService.getScriptProperties().setProperty("DRIVE_FOLDER_ID", String(id).trim());
  Logger.log("DRIVE_FOLDER_ID diset. Jalankan exportCompleted() untuk mengekspor hasil yang tertunda.");
}

function setAdminEmails(emails) {
  PropertiesService.getScriptProperties().setProperty("ADMIN_EMAILS", String(emails).trim());
  Logger.log("ADMIN_EMAILS = " + emails);
}

function setMaxParallel(n) {
  n = Number(n);
  if (!n || n < 1) n = 1;
  PropertiesService.getScriptProperties().setProperty("MAX_PARALLEL", String(n));
  Logger.log("MAX_PARALLEL = " + n);
  return n;
}

/**
 * Bootstrap user admin pertama (dijalankan sekali dari editor Apps Script).
 * Membuat/reset API key untuk email admin, lalu mencetaknya ke Execution log.
 * Salin "API KEY: ..." dan tempel di kolom API key dashboard.
 */
function bootstrapAdmin() {
  tab_("Users", USER_HEADERS);
  tab_("Jobs", JOB_HEADERS);
  tab_("Logs", LOG_HEADERS);
  syncHeaders_("Users", USER_HEADERS);
  syncHeaders_("Jobs", JOB_HEADERS);
  syncHeaders_("Logs", LOG_HEADERS);

  var email = String(prop_("ADMIN_EMAILS", "renditobloggerspot@gmail.com")).split(",")[0].trim();
  if (!email) throw new Error("ADMIN_EMAILS kosong. Jalankan setAdminEmails('renditobloggerspot@gmail.com') dulu.");

  var res = adminCreateKey_(email, 0);
  Logger.log("============================================================");
  Logger.log("ADMIN   : " + res.email);
  Logger.log("API KEY : " + res.api_key);
  Logger.log("Simpan key ini (hanya tampil di log). Tempel ke dashboard.");
  Logger.log("============================================================");
  return res.api_key;
}

/** Hash sebuah API key (untuk debug / verifikasi manual). */
function hashApiKey(key) {
  var h = hashKey_(key);
  Logger.log("hash(" + key + ") = " + h);
  return h;
}

/** Cek user terdaftar atau belum. */
function listApiUsers() {
  var users = readTable_("Users", USER_HEADERS);
  Logger.log("Total users: " + users.length);
  users.forEach(function (u) {
    Logger.log("- " + u.email + " | quota=" + u.quota + " | used=" + u.used + " | active=" + u.active);
  });
  return users;
}

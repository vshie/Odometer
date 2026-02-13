# Odometer Repository Review – Findings

**Review date:** 2025-02-11  
**Status:** All actionable issues have been fixed.

---

## Critical Issues (FIXED)

### 1. **`request.json` can be None – AttributeError risk** ✓ Fixed
**Files:** `app/main.py` (lines 1872, 1914)

`update_maintenance` and `delete_maintenance` use `data = request.json` without a fallback. If the request has invalid JSON or wrong Content-Type, `request.json` is `None` and `data.get()` raises `AttributeError`.

**Fix applied:** `data = request.json or {}` in both `update_maintenance` and `delete_maintenance`.

---

### 2. **Dockerfile label typo** ✓ Fixed
**File:** `Dockerfile` (line 69)

```dockerfile
"email": "$tony@bluerobotics.com"
```

The `$` is literal; the `AUTHOR_EMAIL` ARG is not used. The label will show `$tony@bluerobotics.com` instead of the intended value.

**Fix applied:** `"email": "${AUTHOR_EMAIL}"` with default `ARG AUTHOR_EMAIL=tony@bluerobotics.com`.

---

## Medium Issues (FIXED)

### 3. **`save_modes` holds lock during I/O** ✓ Fixed
**File:** `app/main.py` – `save_modes()`

The method holds `stats_lock` while building the data dict. The lock is released before `json.dump`, but `mode_minutes` is a mutable dict; another thread could modify it between the copy and the write. Use a deep copy so the snapshot is immutable:

```python
data = {'mode_minutes': dict(self.stats.get('mode_minutes', {}))}
```

**Fix applied:** Deep copy with `dict(self.stats.get('mode_minutes', {}))`.

---

### 4. **Dependency mismatch** ✓ Fixed
**Files:** `Dockerfile` vs `app/pyproject.toml`

- Dockerfile installs: `flask`, `werkzeug`, `requests`, `websockets`, `reportlab`
- pyproject.toml lists: `requests`, `flask`, `werkzeug`, `reportlab` (no `websockets`)

`websockets` is used in `main.py` but not declared in pyproject.toml.

**Fix applied:** Added `websockets>=10.0` to pyproject.toml.

---

### 5. **`clear_history` and CSV format**
**File:** `app/main.py` – `clear_history()`

`clear_history` assumes voltage at index 7, depth at 8, cpu_temp at 9. The current odometer CSV has `total_distance_m` at index 12. The indices used for voltage/depth/cpu_temp are correct for the format with `dive_minutes`, but the logic does not handle the `total_distance_m` column. It only clears voltage, depth, and cpu_temp, which is fine; no change needed for `total_distance_m`.

---

## Low / Informational

### 6. **Path traversal in catch-all route** ✓ Fixed
**File:** `app/main.py` – `catch_all()`

Added explicit path validation using `os.path.commonpath()` to ensure the requested path is within the static folder before serving.

---

### 7. **Missing MAINTENANCE_CSV header** ✓ Fixed
**File:** `app/main.py` – `add_maintenance()`

Added defensive check: if MAINTENANCE_CSV doesn't exist or is empty, call `setup_csv_files()` before appending to ensure headers are present.

---

### 8. **`register_service` file**
**File:** `app/static/register_service`

Required for BlueOS as documented. No change needed.

---

### 9. **Log file name** ✓ Fixed
**File:** `app/main.py`

Changed `lumber.log` to `odometer.log`.

---

## Summary

| Severity | Count |
|----------|-------|
| Critical | 2     |
| Medium   | 2     |
| Low      | 4     |

All items have been addressed.

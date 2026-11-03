#!/usr/bin/env python3
"""
check_and_catalog.py

Phase 1 of Antigravity 2.5.x to 2.6.0 Migration:
- Verifies directory health and required files across Linux, macOS, and Windows.
- Identifies active projects in Antigravity Desktop / IDE (including ~/.gemini/config/projects).
- Resolves symlinks (os.path.realpath) and URL-encoding differences (%20, %40) to deduplicate split projects.
- Catalogs conversations (.db and .pb), brain folders, and missing 2.6.0 title fields.
- Generates `scratch_file.yml` for human/agent collaborative review.
"""

import os
import re
import sys
import json
import glob
import sqlite3
import argparse
import urllib.parse
from typing import Dict, List, Set, Tuple, Any
import yaml

# Daftar ID proyek internal yang bukan merupakan UUID proyek pengguna
IGNORED_PROJECT_IDS = {"", "default-cli-project", "outside-of-project"}
UUID_PATTERN = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def get_default_config_dir() -> str:
    # Menentukan direktori konfigurasi bawaan berdasarkan sistem operasi
    if sys.platform == "darwin":
        ide_dir = os.path.expanduser("~/Library/Application Support/Antigravity IDE")
        std_dir = os.path.expanduser("~/Library/Application Support/Antigravity")
        return ide_dir if os.path.isdir(ide_dir) else std_dir
    elif sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        return os.path.join(appdata, "Antigravity") if appdata else os.path.expanduser("~/.config/Antigravity")
    return os.path.expanduser("~/.config/Antigravity")


def normalize_workspace_uri(uri_or_path: str) -> str:
    # Menormalisasi URI file:// dan mendekode karakter URL seperti %20 atau %40 agar konsisten
    if not uri_or_path:
        return ""
    cleaned = uri_or_path.strip()
    if cleaned.startswith("file://"):
        cleaned = cleaned[len("file://"):]
    cleaned = urllib.parse.unquote(cleaned)
    return cleaned.rstrip("/")


def resolve_real_workspace_path(norm_path: str) -> str:
    # Menyelesaikan symlink ke path fisik sebenarnya (realpath) jika path tersebut ada di sistem berkas
    if not norm_path:
        return ""
    try:
        if os.path.exists(norm_path) or os.path.islink(norm_path):
            return os.path.realpath(norm_path).rstrip("/")
    except Exception:
        pass
    return norm_path


DEFAULT_OLD_IDE_DIR = os.path.expanduser("~/.gemini/antigravity-ide")
DEFAULT_DESTINATION_DIR = os.path.expanduser("~/.gemini/antigravity")
DEFAULT_CONFIG_DIR = get_default_config_dir()
DEFAULT_PROJECTS_CONFIG_DIR = os.path.expanduser("~/.gemini/config/projects")
DEFAULT_OUTPUT_YAML = "scratch_file.yml"


def parse_args():
    # Menguraikan argumen baris perintah untuk proses katalogisasi
    parser = argparse.ArgumentParser(description="Antigravity Migration: Check and Catalog")
    parser.add_argument("--old-ide-dir", default=DEFAULT_OLD_IDE_DIR, help="Path to 2.5.x antigravity-ide source directory")
    parser.add_argument("--destination-dir", default=DEFAULT_DESTINATION_DIR, help="Path to 2.6.0 destination directory")
    parser.add_argument("--config-dir", default=DEFAULT_CONFIG_DIR, help="Path to Antigravity Desktop config directory")
    parser.add_argument("--projects-config-dir", default=DEFAULT_PROJECTS_CONFIG_DIR, help="Path to ~/.gemini/config/projects directory")
    parser.add_argument("--output", default=DEFAULT_OUTPUT_YAML, help="Output YAML file path (default: scratch_file.yml)")
    return parser.parse_args()


def validate_environment(old_dir: str, dest_dir: str) -> None:
    # Memvalidasi keberadaan direktori sumber, tujuan, serta file indeks utama
    print("--- 1. Validating Directories ---")
    if not os.path.isdir(old_dir):
        print(f"ERROR: Source directory not found: {old_dir}", file=sys.stderr)
        sys.exit(1)
    print(f"✓ Source directory found: {old_dir}")

    if not os.path.isdir(dest_dir):
        print(f"ERROR: Destination directory not found: {dest_dir}", file=sys.stderr)
        sys.exit(1)
    print(f"✓ Destination directory found: {dest_dir}")

    db_path = os.path.join(dest_dir, "conversation_summaries.db")
    if not os.path.isfile(db_path):
        print(f"ERROR: Destination is missing conversation_summaries.db at {db_path}", file=sys.stderr)
        print("Please ensure Antigravity Desktop/IDE 2.6.0 has initialized the destination directory.", file=sys.stderr)
        sys.exit(1)
    print(f"✓ Found conversation_summaries.db: {db_path}")

    proto_path = os.path.join(dest_dir, "agyhub_summaries_proto.pb")
    if not os.path.isfile(proto_path):
        print(f"WARNING: Destination is missing agyhub_summaries_proto.pb at {proto_path}", file=sys.stderr)
    else:
        print(f"✓ Found agyhub_summaries_proto.pb: {proto_path}")


def load_projects_metadata(
    config_dir: str,
    projects_config_dir: str,
    dest_db_path: str
) -> Tuple[List[Dict[str, str]], Dict[str, str], Dict[str, str]]:
    """Memuat daftar proyek aktif, memetakan path & symlink (realpath) ke UUID kanonis, serta mencatat proyek duplikat."""
    print("\n--- 2. Cataloging Active Projects ---")

    # 1. Membaca pemetaan nama proyek dari ~/.gemini/projects.json jika tersedia
    name_map: Dict[str, str] = {}
    gemini_projects_json = os.path.expanduser("~/.gemini/projects.json")
    if os.path.isfile(gemini_projects_json):
        try:
            with open(gemini_projects_json, "r", encoding="utf-8") as f:
                pdata = json.load(f)
                name_map = {normalize_workspace_uri(k): v for k, v in pdata.get("projects", {}).items()}
        except Exception:
            pass

    # 2. Membaca definisi proyek resmi 2.6.0 dari ~/.gemini/config/projects/*.json
    pid_to_name_config: Dict[str, str] = {}
    pid_to_uris: Dict[str, Set[str]] = {}
    realpath_to_pids: Dict[str, List[str]] = {}

    if os.path.isdir(projects_config_dir):
        for pfile in sorted(glob.glob(os.path.join(projects_config_dir, "*.json"))):
            try:
                with open(pfile, "r", encoding="utf-8") as pf:
                    pdoc = json.load(pf)
                pid = pdoc.get("id", "")
                if not pid or pid in IGNORED_PROJECT_IDS:
                    continue
                pname = urllib.parse.unquote(pdoc.get("name", "")).strip()
                if pname:
                    pid_to_name_config[pid] = pname
                resources = pdoc.get("projectResources", {}).get("resources", [])
                for res in resources:
                    raw_uri = res.get("gitFolder", {}).get("folderUri") or res.get("folderUri") or ""
                    norm_uri = normalize_workspace_uri(raw_uri)
                    if norm_uri:
                        pid_to_uris.setdefault(pid, set()).add(norm_uri)
                        rpath = resolve_real_workspace_path(norm_uri)
                        if pid not in realpath_to_pids.setdefault(rpath, []):
                            realpath_to_pids[rpath].append(pid)
            except Exception:
                pass

    # 3. Menyatukan proyek duplikat yang menunjuk ke realpath yang sama (misal akibat symlink di ~/Antigravity)
    duplicate_pid_to_canonical: Dict[str, str] = {}
    for rpath, pids in realpath_to_pids.items():
        if len(pids) > 1:
            # Prioritaskan proyek yang namanya tidak berakhiran ' 2' atau yang memiliki path di ~/Antigravity
            def sort_key(p: str):
                pname = pid_to_name_config.get(p, "")
                has_suffix_2 = 1 if re.search(r"\s+\d+$", pname) else 0
                return (has_suffix_2, pname, p)

            sorted_pids = sorted(pids, key=sort_key)
            canonical_pid = sorted_pids[0]
            for dup_pid in sorted_pids[1:]:
                duplicate_pid_to_canonical[dup_pid] = canonical_pid
                # Gabungkan seluruh URI milik duplikat ke dalam proyek kanonis
                pid_to_uris.setdefault(canonical_pid, set()).update(pid_to_uris.get(dup_pid, set()))

    # 4. Membaca urutan proyek dari app_storage.json jika tersedia
    ordered_ids: List[str] = []
    app_storage_path = os.path.join(config_dir, "app_storage.json")
    if os.path.isfile(app_storage_path):
        try:
            with open(app_storage_path, "r", encoding="utf-8") as f:
                storage = json.load(f)
                ordered_ids = json.loads(storage.get("projectsOrder", "[]"))
        except Exception:
            pass

    # 5. Mengambil pemetaan project_id dan workspace_uris dari SQLite
    con = sqlite3.connect(dest_db_path)
    cur = con.cursor()
    rows = cur.execute("""
        SELECT project_id, workspace_uris, title, max(last_modified_time)
        FROM conversation_summaries
        WHERE project_id != '' AND project_id != 'default-cli-project' AND project_id != 'outside-of-project'
        GROUP BY project_id, workspace_uris
    """).fetchall()
    con.close()

    for pid, uris_raw, title, _ in rows:
        if pid in IGNORED_PROJECT_IDS:
            continue
        canonical_pid = duplicate_pid_to_canonical.get(pid, pid)
        try:
            uris = json.loads(uris_raw)
            for u in uris:
                clean_path = normalize_workspace_uri(u)
                if clean_path:
                    pid_to_uris.setdefault(canonical_pid, set()).add(clean_path)
        except Exception:
            pass

    # Membangun tabel pencarian uri_to_pid (mencakup path asli maupun realpath hasil resolusi symlink)
    uri_to_pid: Dict[str, str] = {}
    for pid, uris in pid_to_uris.items():
        if pid in duplicate_pid_to_canonical:
            continue
        for u in uris:
            uri_to_pid[u] = pid
            rpath = resolve_real_workspace_path(u)
            if rpath:
                uri_to_pid[rpath] = pid

    # Menggabungkan semua ID proyek kanonis secara berurutan tanpa duplikasi
    projects_list: List[Dict[str, str]] = []
    seen_pids: Set[str] = set()
    all_pids = ordered_ids + [p for p in pid_to_name_config.keys() if p not in ordered_ids] + [p for p in pid_to_uris.keys() if p not in ordered_ids]

    for pid in all_pids:
        pid = duplicate_pid_to_canonical.get(pid, pid)
        if pid in seen_pids or pid in IGNORED_PROJECT_IDS:
            continue
        seen_pids.add(pid)

        uris = pid_to_uris.get(pid, set())
        name = pid_to_name_config.get(pid, "")
        if not name:
            if uris:
                primary_path = sorted(list(uris))[0]
                if primary_path in name_map:
                    name = name_map[primary_path]
                else:
                    name = os.path.basename(primary_path) or primary_path
            else:
                name = "Unknown Project"

        projects_list.append({
            "name": name,
            "id": pid
        })

    print(
        f"Cataloged {len(projects_list)} active canonical projects "
        f"({len(duplicate_pid_to_canonical)} symlink-duplicate projects merged, {len(uri_to_pid)} mapped paths)."
    )
    return projects_list, uri_to_pid, duplicate_pid_to_canonical


def infer_workspace_from_transcript(tpath: str, known_paths: List[str]) -> str:
    # Menebak path workspace dari transcript.jsonl menggunakan pencocokan prefix terpanjang atau penanda .git
    if not os.path.isfile(tpath):
        return "Unknown/Unassociated"
    try:
        with open(tpath, "r", encoding="utf-8", errors="ignore") as tf:
            for line in tf:
                if "@[" in line:
                    match = re.search(r"@\[([^\]]+)\]", line)
                    if match:
                        fpath = normalize_workspace_uri(match.group(1))
                        rpath = resolve_real_workspace_path(fpath)
                        for candidate in (fpath, rpath):
                            for kp in sorted(known_paths, key=len, reverse=True):
                                if candidate == kp or candidate.startswith(kp + "/"):
                                    return kp
                        curr = fpath if os.path.isdir(fpath) else os.path.dirname(fpath)
                        while curr and curr != "/" and len(curr.split("/")) > 3:
                            if os.path.isdir(os.path.join(curr, ".git")):
                                return curr
                            curr = os.path.dirname(curr)
                    break
    except Exception:
        pass
    return "Unknown/Unassociated"


def catalog_conversations(
    old_dir: str,
    dest_dir: str,
    projects_list: List[Dict[str, str]],
    uri_to_pid: Dict[str, str],
    duplicate_pid_to_canonical: Dict[str, str]
) -> List[Dict[str, Any]]:
    # Memeriksa status migrasi setiap percakapan (.db, .pb, folder brain, dan kolom title)
    print("\n--- 3. Auditing Conversations & Migration State ---")

    old_conv_dir = os.path.join(old_dir, "conversations")
    old_brain_dir = os.path.join(old_dir, "brain")

    dest_conv_dir = os.path.join(dest_dir, "conversations")
    dest_brain_dir = os.path.join(dest_dir, "brain")
    dest_db_path = os.path.join(dest_dir, "conversation_summaries.db")

    con = sqlite3.connect(dest_db_path)
    cur = con.cursor()

    # Memuat metadata ringkasan percakapan dari SQLite tujuan
    dest_records: Dict[str, Dict[str, Any]] = {}
    for cid, uris_raw, pid, title, preview in cur.execute(
        "SELECT conversation_id, workspace_uris, project_id, title, preview FROM conversation_summaries"
    ):
        dest_records[cid] = {
            "workspace_uris": uris_raw,
            "project_id": pid,
            "title": title or "",
            "preview": preview or ""
        }
    con.close()

    # Mendeteksi seluruh file percakapan (.db maupun .pb) serta folder brain berformat UUID
    old_conv_files = os.listdir(old_conv_dir) if os.path.isdir(old_conv_dir) else []
    source_dbs = {f[:-3] for f in old_conv_files if f.endswith(".db")}
    source_pbs = {f[:-3] for f in old_conv_files if f.endswith(".pb")}
    source_convs = source_dbs.union(source_pbs)
    source_done = {f[1:-5] for f in old_conv_files if f.startswith(".") and f.endswith(".done")}
    source_brains = {
        f for f in os.listdir(old_brain_dir)
        if UUID_PATTERN.match(f) and os.path.isdir(os.path.join(old_brain_dir, f))
    } if os.path.isdir(old_brain_dir) else set()

    all_cids = sorted(list(source_convs.union(source_brains)))
    print(
        f"Found {len(source_dbs)} .db files, {len(source_pbs)} .pb files, "
        f"{len(source_done)} .done markers, {len(source_brains)} brain folders in source."
    )

    folder_groups: Dict[str, List[Dict[str, Any]]] = {}
    known_paths = list(uri_to_pid.keys())

    for cid in all_cids:
        has_src_conv = cid in source_convs
        is_src_done = cid in source_done
        has_src_brain = cid in source_brains

        has_dst_conv = (
            os.path.isfile(os.path.join(dest_conv_dir, f"{cid}.db"))
            or os.path.isfile(os.path.join(dest_conv_dir, f"{cid}.pb"))
        )
        has_dst_brain = os.path.isdir(os.path.join(dest_brain_dir, cid))
        dest_meta = dest_records.get(cid)

        workspace_path = "Unknown/Unassociated"
        assigned_pid = ""
        title_populated = True
        if dest_meta:
            raw_pid = dest_meta["project_id"] or ""
            assigned_pid = "" if raw_pid in IGNORED_PROJECT_IDS else raw_pid
            if dest_meta["preview"] and not dest_meta["title"]:
                title_populated = False
            uris_raw = dest_meta["workspace_uris"] or ""
            if uris_raw and uris_raw != "[]":
                try:
                    uris = json.loads(uris_raw)
                    if uris:
                        workspace_path = normalize_workspace_uri(uris[0])
                except Exception:
                    workspace_path = normalize_workspace_uri(uris_raw)

        if workspace_path == "Unknown/Unassociated" and has_src_brain:
            tpath = os.path.join(old_brain_dir, cid, ".system_generated", "logs", "transcript.jsonl")
            workspace_path = infer_workspace_from_transcript(tpath, known_paths)

        # Menentukan apakah percakapan tidak dapat dimigrasikan (tidak memiliki file .db/.pb di sumber maupun tujuan)
        is_unmigratable = (not has_src_conv and not has_dst_conv)

        # Menentukan apakah PID yang terpasang saat ini sudah merupakan PID kanonis (bukan duplikat symlink)
        is_canonical_pid = bool(assigned_pid) and (assigned_pid not in duplicate_pid_to_canonical)
        brain_satisfied = has_dst_brain or (not has_src_brain)
        pid_satisfied = is_canonical_pid or (workspace_path == "Unknown/Unassociated")
        is_fully_migrated = (
            has_dst_conv
            and brain_satisfied
            and dest_meta is not None
            and pid_satisfied
            and title_populated
        )

        folder_groups.setdefault(workspace_path, []).append({
            "cid": cid,
            "has_src_conv": has_src_conv,
            "is_src_done": is_src_done,
            "has_dst_conv": has_dst_conv,
            "has_src_brain": has_src_brain,
            "has_dst_brain": has_dst_brain,
            "assigned_pid": assigned_pid,
            "is_fully_migrated": is_fully_migrated,
            "is_unmigratable": is_unmigratable,
        })

    # Menyusun daftar backfill untuk ditulis ke manifes YAML
    backfills_list: List[Dict[str, Any]] = []
    name_to_pid = {p["name"].lower(): p["id"] for p in projects_list}

    for folder, convs in sorted(folder_groups.items(), key=lambda x: -len(x[1])):
        total = len(convs)
        fully_migrated = sum(1 for c in convs if c["is_fully_migrated"])
        unmigratable = sum(1 for c in convs if c["is_unmigratable"])
        migratable = total - fully_migrated - unmigratable

        assigned_pids = {
            duplicate_pid_to_canonical.get(c["assigned_pid"], c["assigned_pid"])
            for c in convs if c["assigned_pid"]
        }
        project_assigned = (
            len(assigned_pids) == 1
            and all(bool(c["assigned_pid"]) and c["assigned_pid"] not in duplicate_pid_to_canonical for c in convs)
        )

        # Menyarankan UUID proyek kanonis: cek uri_to_pid (termasuk realpath symlink), lalu PID terpasang, lalu nama folder
        suggested_pid = ""
        if folder != "Unknown/Unassociated":
            rpath = resolve_real_workspace_path(folder)
            if folder in uri_to_pid:
                suggested_pid = uri_to_pid[folder]
            elif rpath in uri_to_pid:
                suggested_pid = uri_to_pid[rpath]
            elif len(assigned_pids) == 1:
                suggested_pid = list(assigned_pids)[0]
            else:
                base = os.path.basename(folder).lower()
                if base in name_to_pid:
                    suggested_pid = name_to_pid[base]

        backfills_list.append({
            "folder": folder,
            "conversationsCount": {
                "total": total,
                "fullyMigrated": fully_migrated,
                "migratable": migratable,
                "unmigratable": unmigratable
            },
            "projectAssigned": project_assigned,
            "projectIdToAssign": suggested_pid if suggested_pid else ""
        })

    print(f"Cataloged {len(backfills_list)} workspace folders across {len(all_cids)} conversations.")
    return backfills_list


def main():
    args = parse_args()
    print("==================================================")
    print("   Antigravity 2.5.x -> 2.6.0 Migration Catalog")
    print("==================================================")
    print(f"Source (Old IDE) : {args.old_ide_dir}")
    print(f"Destination      : {args.destination_dir}")
    print(f"Config           : {args.config_dir}")
    print(f"Projects Config  : {args.projects_config_dir}")
    print(f"Output File      : {args.output}\n")

    validate_environment(args.old_ide_dir, args.destination_dir)
    projects, uri_to_pid, dup_map = load_projects_metadata(
        args.config_dir,
        args.projects_config_dir,
        os.path.join(args.destination_dir, "conversation_summaries.db")
    )
    backfills = catalog_conversations(args.old_ide_dir, args.destination_dir, projects, uri_to_pid, dup_map)

    yaml_data = {
        "projects": projects,
        "backfills": backfills
    }

    # Menulis hasil katalog ke file YAML
    with open(args.output, "w", encoding="utf-8") as yf:
        yaml.dump(yaml_data, yf, sort_keys=False, default_flow_style=False, allow_unicode=True)

    print(f"\n✓ Successfully generated manifest: {args.output}")
    print("\nNext step: Review and adjust 'projectIdToAssign' in the manifest before running execute_backfill.py.")


if __name__ == "__main__":
    main()

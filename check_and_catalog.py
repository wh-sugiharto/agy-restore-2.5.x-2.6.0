#!/usr/bin/env python3
"""
check_and_catalog.py

Phase 1 of Antigravity 2.5.x -> 2.6.0 Migration:
- Verifies directory health and required files.
- Identifies active projects in Antigravity Desktop.
- Catalogs conversations by workspace directory.
- Generates `scratch_file.yml` for human/agent collaborative review.
"""

import os
import sys
import json
import sqlite3
import argparse
from typing import Dict, List, Set, Any
import yaml

def get_default_config_dir() -> str:
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/Antigravity")
    elif sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        return os.path.join(appdata, "Antigravity") if appdata else os.path.expanduser("~/.config/Antigravity")
    return os.path.expanduser("~/.config/Antigravity")

DEFAULT_OLD_IDE_DIR = os.path.expanduser("~/.gemini/antigravity-ide")
DEFAULT_DESTINATION_DIR = os.path.expanduser("~/.gemini/antigravity")
DEFAULT_CONFIG_DIR = get_default_config_dir()
DEFAULT_OUTPUT_YAML = "scratch_file.yml"

def parse_args():
    parser = argparse.ArgumentParser(description="Antigravity Migration: Check and Catalog")
    parser.add_argument("--old-ide-dir", default=DEFAULT_OLD_IDE_DIR, help="Path to 2.5.x antigravity-ide source directory")
    parser.add_argument("--destination-dir", default=DEFAULT_DESTINATION_DIR, help="Path to 2.6.0 destination directory")
    parser.add_argument("--config-dir", default=DEFAULT_CONFIG_DIR, help="Path to Antigravity Desktop config directory")
    parser.add_argument("--output", default=DEFAULT_OUTPUT_YAML, help="Output YAML file path (default: scratch_file.yml)")
    return parser.parse_args()

def validate_environment(old_dir: str, dest_dir: str) -> None:
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

def load_projects_metadata(config_dir: str, dest_db_path: str) -> List[Dict[str, str]]:
    """Loads active projects list with human names and UUIDs."""
    print("\n--- 2. Cataloging Active Projects ---")
    
    # 1. Check projects.json in ~/.gemini/projects.json for name mapping if exists
    name_map = {}
    gemini_projects_json = os.path.expanduser("~/.gemini/projects.json")
    if os.path.isfile(gemini_projects_json):
        try:
            with open(gemini_projects_json, "r", encoding="utf-8") as f:
                pdata = json.load(f)
                name_map = {k.rstrip('/'): v for k, v in pdata.get("projects", {}).items()}
        except Exception:
            pass

    # 2. Check projectsOrder in app_storage.json
    ordered_ids = []
    app_storage_path = os.path.join(config_dir, "app_storage.json")
    if os.path.isfile(app_storage_path):
        try:
            with open(app_storage_path, "r", encoding="utf-8") as f:
                storage = json.load(f)
                ordered_ids = json.loads(storage.get("projectsOrder", "[]"))
        except Exception:
            pass

    # 3. Query SQLite for project_id to workspace mappings
    con = sqlite3.connect(dest_db_path)
    cur = con.cursor()
    rows = cur.execute("""
        SELECT project_id, workspace_uris, title, max(last_modified_time)
        FROM conversation_summaries
        WHERE project_id != '' AND project_id != 'default-cli-project'
        GROUP BY project_id, workspace_uris
    """).fetchall()
    con.close()

    pid_to_uris: Dict[str, Set[str]] = {}
    for pid, uris_raw, title, _ in rows:
        try:
            uris = json.loads(uris_raw)
            for u in uris:
                clean_path = u.replace("file://", "").rstrip("/")
                pid_to_uris.setdefault(pid, set()).add(clean_path)
        except Exception:
            pass

    # Combine into ProjectEntries
    projects_list = []
    seen_pids = set()

    # Process ordered IDs first
    all_pids = ordered_ids + [p for p in pid_to_uris.keys() if p not in ordered_ids]

    for pid in all_pids:
        if pid in seen_pids or pid == "default-cli-project":
            continue
        seen_pids.add(pid)

        uris = pid_to_uris.get(pid, set())
        # Infer best name
        name = "Unknown Project"
        if uris:
            primary_path = sorted(list(uris))[0]
            if primary_path in name_map:
                name = name_map[primary_path]
            else:
                name = os.path.basename(primary_path) or primary_path

        projects_list.append({
            "name": name,
            "id": pid
        })

    print(f"Cataloged {len(projects_list)} active projects.")
    return projects_list

def catalog_conversations(old_dir: str, dest_dir: str, projects_list: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    print("\n--- 3. Auditing Conversations & Migration State ---")
    
    old_conv_dir = os.path.join(old_dir, "conversations")
    old_brain_dir = os.path.join(old_dir, "brain")

    dest_conv_dir = os.path.join(dest_dir, "conversations")
    dest_brain_dir = os.path.join(dest_dir, "brain")
    dest_db_path = os.path.join(dest_dir, "conversation_summaries.db")

    con = sqlite3.connect(dest_db_path)
    cur = con.cursor()

    # Load destination records
    dest_records = {}
    for cid, uris_raw, pid, preview in cur.execute("SELECT conversation_id, workspace_uris, project_id, preview FROM conversation_summaries"):
        dest_records[cid] = {
            "workspace_uris": uris_raw,
            "project_id": pid,
            "preview": preview
        }
    con.close()

    # Discover all conversations in source
    source_dbs = {f[:-3] for f in os.listdir(old_conv_dir) if f.endswith(".db")} if os.path.isdir(old_conv_dir) else set()
    source_done = {f[1:-5] for f in os.listdir(old_conv_dir) if f.startswith(".") and f.endswith(".done")} if os.path.isdir(old_conv_dir) else set()
    source_brains = set(os.listdir(old_brain_dir)) if os.path.isdir(old_brain_dir) else set()

    all_cids = sorted(list(source_dbs.union(source_brains)))
    print(f"Found {len(source_dbs)} .db files, {len(source_done)} .done markers, {len(source_brains)} brain folders in source.")

    # Group conversations by folder
    # Map workspace normalized path -> list of conversation statuses
    folder_groups: Dict[str, List[Dict[str, Any]]] = {}

    # Build quick lookup for projects: normalized_path -> pid
    # Also inverse: pid -> project_name
    pid_to_name = {p["id"]: p["name"] for p in projects_list}

    # Extract workspace paths for each conversation
    for cid in all_cids:
        # Check source presence
        has_src_db = cid in source_dbs
        is_src_done = cid in source_done
        has_src_brain = cid in source_brains

        # Check dest presence
        has_dst_db = os.path.isfile(os.path.join(dest_conv_dir, f"{cid}.db"))
        has_dst_brain = os.path.isdir(os.path.join(dest_brain_dir, cid))
        dest_meta = dest_records.get(cid)

        # Determine workspace path
        workspace_path = "Unknown/Unassociated"
        assigned_pid = ""
        if dest_meta and dest_meta["workspace_uris"]:
            try:
                uris = json.loads(dest_meta["workspace_uris"])
                if uris:
                    workspace_path = uris[0].replace("file://", "").rstrip("/")
            except Exception:
                workspace_path = dest_meta["workspace_uris"].replace("file://", "").rstrip("/")
            assigned_pid = dest_meta["project_id"]
        elif has_src_brain:
            # Try to read transcript first line to inspect workspace
            tpath = os.path.join(old_brain_dir, cid, ".system_generated", "logs", "transcript.jsonl")
            if os.path.isfile(tpath):
                try:
                    with open(tpath, "r", encoding="utf-8") as tf:
                        for line in tf:
                            if "@[" in line:
                                import re
                                match = re.search(r"@\[([^]]+)\]", line)
                                if match:
                                    fpath = match.group(1)
                                    # Guess project folder from file path
                                    parts = fpath.split("/")
                                    if len(parts) > 4:
                                        workspace_path = "/".join(parts[:5])
                                break
                except Exception:
                    pass

        # Classify status
        # fullyMigrated: dst_db present, dst_brain present, indexed in db, project_id assigned
        # migratable: src_db present or src_brain present, but missing pieces in destination
        # unmigratable: brain only with no transcript or completely corrupt
        is_unmigratable = False
        if not has_src_db and not has_dst_db:
            # Check if brain has transcript
            tpath = os.path.join(old_brain_dir, cid, ".system_generated", "logs", "transcript.jsonl")
            if not os.path.isfile(tpath) or os.path.getsize(tpath) == 0:
                is_unmigratable = True

        is_fully_migrated = (has_dst_db and has_dst_brain and dest_meta is not None and bool(assigned_pid))

        folder_groups.setdefault(workspace_path, []).append({
            "cid": cid,
            "has_src_db": has_src_db,
            "is_src_done": is_src_done,
            "has_dst_db": has_dst_db,
            "has_dst_brain": has_dst_brain,
            "assigned_pid": assigned_pid,
            "is_fully_migrated": is_fully_migrated,
            "is_unmigratable": is_unmigratable,
        })

    # Build BackfillEntries
    backfills_list = []

    # Map project names to PIDs for matching
    name_to_pid = {p["name"].lower(): p["id"] for p in projects_list}

    for folder, convs in sorted(folder_groups.items(), key=lambda x: -len(x[1])):
        total = len(convs)
        fully_migrated = sum(1 for c in convs if c["is_fully_migrated"])
        unmigratable = sum(1 for c in convs if c["is_unmigratable"])
        migratable = total - fully_migrated - unmigratable

        # Check project assignment status
        assigned_pids = {c["assigned_pid"] for c in convs if c["assigned_pid"]}
        project_assigned = (len(assigned_pids) == 1 and "" not in [c["assigned_pid"] for c in convs])

        # Suggest project ID to assign
        suggested_pid = ""
        if len(assigned_pids) == 1:
            suggested_pid = list(assigned_pids)[0]
        else:
            # Try to match folder basename against known project names
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
    print(f"Output File      : {args.output}\n")

    validate_environment(args.old_ide_dir, args.destination_dir)
    projects = load_projects_metadata(args.config_dir, os.path.join(args.destination_dir, "conversation_summaries.db"))
    backfills = catalog_conversations(args.old_ide_dir, args.destination_dir, projects)

    yaml_data = {
        "projects": projects,
        "backfills": backfills
    }

    # Write output YAML
    with open(args.output, "w", encoding="utf-8") as yf:
        yaml.dump(yaml_data, yf, sort_keys=False, default_flow_style=False, allow_unicode=True)

    print(f"\n✓ Successfully generated manifest: {args.output}")
    print("\nNext step: Review and adjust 'projectIdToAssign' in the manifest before running execute_backfill.py.")

if __name__ == "__main__":
    main()

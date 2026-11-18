#!/usr/bin/env bash
# Owner-run: create the GitHub release that hosts the trusted ontology files and upload
# them as assets, byte for byte. Idempotent: re-running creates nothing twice and uploads
# only the assets that are missing (or, with --clobber, replaces all).
#
#   scripts/publish_ontologies.sh [--dir DIR] [--repo OWNER/REPO] [--tag TAG] [--yes] [--clobber]
#
# DIR is a directory holding the files named in ontologies.json (default: trusted_ontologes/
# of this skill, i.e. a checkout from before the files were removed from git; use
# `git show <old-commit>:...` / a checkout of that commit to obtain them). Every file is
# checked against the manifest sha256 before anything is uploaded. Needs: gh (authenticated
# with write access to the repo), python3, sha256sum.
set -euo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MANIFEST="$SKILL_DIR/ontologies.json"
REPO="sensein/agent_skills"
TAG="structsense-ontologies-v1"
DIR="$SKILL_DIR/trusted_ontologes"
YES=0
CLOBBER=0

while [ $# -gt 0 ]; do
  case "$1" in
    --dir) DIR="$2"; shift 2 ;;
    --repo) REPO="$2"; shift 2 ;;
    --tag) TAG="$2"; shift 2 ;;
    --yes|-y) YES=1; shift ;;
    --clobber) CLOBBER=1; shift ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

command -v gh >/dev/null || { echo "gh is required" >&2; exit 2; }
[ -d "$DIR" ] || { echo "directory not found: $DIR" >&2; exit 2; }

# name<TAB>sha256 per manifest entry
mapfile -t ENTRIES < <(python3 - "$MANIFEST" <<'PY'
import json, sys
for e in json.load(open(sys.argv[1]))["ontologies"]:
    print(f'{e["file"]}\t{e["sha256"]}')
PY
)

echo "Repo: $REPO   Tag: $TAG   Source dir: $DIR   Files in manifest: ${#ENTRIES[@]}"

bad=0
for line in "${ENTRIES[@]}"; do
  name="${line%%$'\t'*}"; want="${line##*$'\t'}"
  if [ ! -f "$DIR/$name" ]; then echo "MISSING locally: $name" >&2; bad=1; continue; fi
  got="$(sha256sum "$DIR/$name" | cut -d' ' -f1)"
  [ "$got" = "$want" ] || { echo "CHECKSUM MISMATCH: $name" >&2; bad=1; }
done
[ "$bad" = 0 ] || { echo "Refusing to publish: fix the files above first." >&2; exit 1; }
echo "All ${#ENTRIES[@]} files match the manifest."

release_exists=0
if gh release view "$TAG" --repo "$REPO" >/dev/null 2>&1; then release_exists=1; fi
existing=""
if [ "$release_exists" = 1 ]; then
  existing="$(gh release view "$TAG" --repo "$REPO" --json assets --jq '.assets[].name')"
fi

todo=()
for line in "${ENTRIES[@]}"; do
  name="${line%%$'\t'*}"
  if [ "$CLOBBER" = 1 ] || ! grep -qxF "$name" <<<"$existing"; then todo+=("$name"); fi
done

echo "Plan:"
if [ "$release_exists" = 1 ]; then echo "  - release $TAG already exists (will not be recreated)"
else echo "  - create release $TAG on $REPO"; fi
echo "  - upload ${#todo[@]} asset(s)$([ "$CLOBBER" = 1 ] && echo ' (replacing existing)'), skip $(( ${#ENTRIES[@]} - ${#todo[@]} )) already present"
for n in "${todo[@]}"; do echo "      $n"; done
[ "${#todo[@]}" -gt 0 ] || [ "$release_exists" = 0 ] || { echo "Nothing to do."; exit 0; }

if [ "$YES" != 1 ]; then
  read -r -p "Proceed? [y/N] " ans
  [ "$ans" = y ] || [ "$ans" = Y ] || { echo "Aborted."; exit 1; }
fi

if [ "$release_exists" = 0 ]; then
  gh release create "$TAG" --repo "$REPO" \
    --title "StructSense trusted ontologies v1" \
    --notes "Trusted ontology files used by skills/structsense, byte for byte. Fetched on demand into a local cache and verified against skills/structsense/ontologies.json (sha256). Several are subsets that upstream does not publish. Do not edit; publish a new tag for new content."
fi

args=()
[ "$CLOBBER" = 1 ] && args+=(--clobber)
for n in "${todo[@]}"; do
  echo "uploading $n"
  gh release upload "$TAG" "$DIR/$n" --repo "$REPO" ${args[@]+"${args[@]}"}
done
echo "Done. Check: python -m scripts.ontology_cache prefetch --all  (with an empty STRUCTSENSE_ONTOLOGY_CACHE)"

#!/usr/bin/env sh
set -eu

# Usage:
#   GH_TOKEN=... scripts/publish_github_release.sh
# Optional:
#   REPO_NAME=Elgato-Key-Lights-for-Linux RELEASE_TAG=v0.2.2 scripts/publish_github_release.sh

if [ -z "${GH_TOKEN:-}" ]; then
  echo "Fehler: GH_TOKEN ist nicht gesetzt."
  echo "Setze einen GitHub Personal Access Token mit 'repo' Rechten."
  exit 1
fi

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT_DIR"

REPO_NAME=${REPO_NAME:-Elgato-Key-Lights-for-Linux}
RELEASE_TAG=${RELEASE_TAG:-v0.2.2}
RELEASE_NAME=${RELEASE_NAME:-Elgato Key Lights for Linux V.0.2.2}
RELEASE_BODY_FILE=${RELEASE_BODY_FILE:-RELEASE_NOTES_v0.2.2.md}

APPIMAGE_FILE="dist/Elgato-Key-Light-Tray-0.2.2-x86_64.AppImage"
PORTABLE_FILE="dist/Elgato-Key-Light-Tray-0.2.2-x86_64-portable.tar.gz"

if [ ! -f "$APPIMAGE_FILE" ] || [ ! -f "$PORTABLE_FILE" ]; then
  echo "Fehler: Release-Artefakte fehlen."
  echo "Erwartet:"
  echo "  $APPIMAGE_FILE"
  echo "  $PORTABLE_FILE"
  exit 1
fi

if [ ! -f "$RELEASE_BODY_FILE" ]; then
  echo "Fehler: Release Notes Datei fehlt: $RELEASE_BODY_FILE"
  exit 1
fi

OWNER=$(curl -fsS -H "Authorization: Bearer $GH_TOKEN" -H "Accept: application/vnd.github+json" https://api.github.com/user | python3 -c 'import json,sys; print(json.load(sys.stdin)["login"])')
if [ -z "$OWNER" ]; then
  echo "Fehler: GitHub Owner konnte nicht ermittelt werden."
  exit 1
fi

echo "GitHub Owner: $OWNER"
echo "Repo: $REPO_NAME"

# Create public repo (idempotent when already exists)
CREATE_CODE=$(curl -sS -o /tmp/create_repo_resp.json -w "%{http_code}" \
  -X POST \
  -H "Authorization: Bearer $GH_TOKEN" \
  -H "Accept: application/vnd.github+json" \
  https://api.github.com/user/repos \
  -d "$(printf '{"name":"%s","description":"%s","private":false}' "$REPO_NAME" "Elgato Key Lights for Linux")")

if [ "$CREATE_CODE" = "201" ]; then
  echo "Repo erstellt: $OWNER/$REPO_NAME"
elif [ "$CREATE_CODE" = "422" ]; then
  echo "Repo existiert bereits: $OWNER/$REPO_NAME"
else
  echo "Fehler beim Repo-Create (HTTP $CREATE_CODE):"
  cat /tmp/create_repo_resp.json
  exit 1
fi

REPO_URL="https://github.com/$OWNER/$REPO_NAME.git"

if git remote get-url origin >/dev/null 2>&1; then
  git remote set-url origin "$REPO_URL"
else
  git remote add origin "$REPO_URL"
fi

# Push using temporary askpass to avoid embedding token in URL
ASKPASS_FILE=$(mktemp)
cat > "$ASKPASS_FILE" <<'ASKPASS'
#!/usr/bin/env sh
case "$1" in
  *Username*) echo "x-access-token" ;;
  *Password*) echo "$GH_TOKEN" ;;
  *) echo "" ;;
esac
ASKPASS
chmod +x "$ASKPASS_FILE"

export GIT_ASKPASS="$ASKPASS_FILE"
export GIT_TERMINAL_PROMPT=0

git push -u origin main
git push origin "$RELEASE_TAG"

rm -f "$ASKPASS_FILE"

# Create or update release
BODY_JSON=$(python3 - <<PY
import json
from pathlib import Path
print(json.dumps(Path("$RELEASE_BODY_FILE").read_text(encoding="utf-8")))
PY
)

REL_CODE=$(curl -sS -o /tmp/release_resp.json -w "%{http_code}" \
  -X POST \
  -H "Authorization: Bearer $GH_TOKEN" \
  -H "Accept: application/vnd.github+json" \
  "https://api.github.com/repos/$OWNER/$REPO_NAME/releases" \
  -d "{\"tag_name\":\"$RELEASE_TAG\",\"name\":\"$RELEASE_NAME\",\"body\":$BODY_JSON,\"draft\":false,\"prerelease\":false}")

if [ "$REL_CODE" = "201" ]; then
  echo "Release erstellt: $RELEASE_TAG"
elif [ "$REL_CODE" = "422" ]; then
  echo "Release existiert bereits, hole bestehende Release-Daten..."
  curl -fsS \
    -H "Authorization: Bearer $GH_TOKEN" \
    -H "Accept: application/vnd.github+json" \
    "https://api.github.com/repos/$OWNER/$REPO_NAME/releases/tags/$RELEASE_TAG" > /tmp/release_resp.json
else
  echo "Fehler beim Release-Create (HTTP $REL_CODE):"
  cat /tmp/release_resp.json
  exit 1
fi

UPLOAD_URL=$(python3 - <<'PY'
import json
with open('/tmp/release_resp.json','r',encoding='utf-8') as f:
    data=json.load(f)
url=data['upload_url']
print(url.split('{',1)[0])
PY
)
RELEASE_ID=$(python3 - <<'PY'
import json
with open('/tmp/release_resp.json','r',encoding='utf-8') as f:
    data=json.load(f)
print(data['id'])
PY
)

# Delete assets with same names if existing
for ASSET in "$(basename "$APPIMAGE_FILE")" "$(basename "$PORTABLE_FILE")"; do
  ASSET_ID=$(curl -fsS \
    -H "Authorization: Bearer $GH_TOKEN" \
    -H "Accept: application/vnd.github+json" \
    "https://api.github.com/repos/$OWNER/$REPO_NAME/releases/$RELEASE_ID/assets" | \
    python3 - <<PY
import json,sys
name = "$ASSET"
assets = json.load(sys.stdin)
for a in assets:
    if a.get('name') == name:
        print(a.get('id'))
        break
PY
)
  if [ -n "${ASSET_ID:-}" ]; then
    curl -fsS -X DELETE \
      -H "Authorization: Bearer $GH_TOKEN" \
      -H "Accept: application/vnd.github+json" \
      "https://api.github.com/repos/$OWNER/$REPO_NAME/releases/assets/$ASSET_ID" >/dev/null
    echo "Altes Asset entfernt: $ASSET"
  fi
done

upload_asset() {
  FILE_PATH="$1"
  FILE_NAME=$(basename "$FILE_PATH")
  curl -fsS \
    -X POST \
    -H "Authorization: Bearer $GH_TOKEN" \
    -H "Content-Type: application/octet-stream" \
    -H "Accept: application/vnd.github+json" \
    --data-binary @"$FILE_PATH" \
    "$UPLOAD_URL?name=$FILE_NAME" >/dev/null
  echo "Asset hochgeladen: $FILE_NAME"
}

upload_asset "$APPIMAGE_FILE"
upload_asset "$PORTABLE_FILE"

echo "Fertig: https://github.com/$OWNER/$REPO_NAME/releases/tag/$RELEASE_TAG"

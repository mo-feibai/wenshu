#!/bin/bash
set -e
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP=/opt/wenshu/app
cp "$SRC"/custom/scripts/*.py $APP/scripts/
cp "$SRC/custom/src/content.config.ts" $APP/src/content.config.ts
cp "$SRC/custom/src/pages/docs/[...id].astro" "$APP/src/pages/docs/[...id].astro"
cp "$SRC/custom/src/pages/topics/[topic].astro" "$APP/src/pages/topics/[topic].astro"
cp "$SRC/custom/src/styles/global.css" $APP/src/styles/global.css
cd $APP
npm run build 2>&1 | grep -E "generated:|files synced:|error|Error" | head -10
echo "--- dist/files count ---"
ls dist/files | wc -l
echo "--- built doc pages ---"
find dist/docs -name index.html | wc -l
echo "--- built topic pages ---"
find dist/topics -name index.html | wc -l

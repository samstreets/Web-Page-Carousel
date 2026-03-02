#!/bin/sh
set -e

# PAGES env var: comma-separated list of URLs
# INTERVAL env var: seconds per page (default 30)

PAGES=${PAGES:-"https://example.com"}
INTERVAL=${INTERVAL:-30}

# Convert comma-separated pages to JSON array
PAGES_JSON="["
IFS=',' read -r -a PAGE_ARRAY <<EOF
$PAGES
EOF

first=true
for page in "${PAGE_ARRAY[@]}"; do
  page=$(echo "$page" | tr -d '[:space:]')
  if [ -n "$page" ]; then
    if [ "$first" = true ]; then
      PAGES_JSON="${PAGES_JSON}\"${page}\""
      first=false
    else
      PAGES_JSON="${PAGES_JSON},\"${page}\""
    fi
  fi
done
PAGES_JSON="${PAGES_JSON}]"

# Write config to be loaded by index.html
cat > /usr/share/nginx/html/config.js <<JSEOF
window.CAROUSEL_CONFIG = {
  pages: ${PAGES_JSON},
  interval: ${INTERVAL}
};
JSEOF

echo "Carousel config written:"
cat /usr/share/nginx/html/config.js

exec nginx -g "daemon off;"

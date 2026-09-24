#!/bin/bash
set -e

# Load environment variables from .env file
ENV_FILE="$(dirname "$0")/.env"

if [[ ! -f "$ENV_FILE" ]]; then
    echo "Error: $ENV_FILE not found"
    exit 1
fi

# Parse .env file (simple key=value, no exports)
while IFS='=' read -r key value; do
    # Skip comments and empty lines
    [[ "$key" =~ ^#.*$ ]] && continue
    [[ -z "$key" ]] && continue
    # Remove leading/trailing whitespace
    key=$(echo "$key" | xargs)
    value=$(echo "$value" | xargs)
    # Assign to variable
    declare "$key=$value"
done < "$ENV_FILE"

# Validate required variables
if [[ -z "$FTP_HOST" || -z "$FTP_USER" || -z "$FTP_PASSWORD" || -z "$FTP_DIR" ]]; then
    echo "Error: Missing required environment variables in .env file"
    exit 1
fi

# Files to upload (all files in landing_page directory)
FILES=(
    "index.html"
    "*.css"
    "*.js"
    "*.png"
    "*.jpg"
    "*.svg"
    "*.ico"
)

echo "Deploying to ftp://$FTP_HOST$FTP_DIR/"
echo "User: $FTP_USER"
echo "Host: $FTP_HOST"
echo ""

for file in "${FILES[@]}"; do
    for f in $(find "$(dirname "$0")" -maxdepth 1 -name "$file" -type f 2>/dev/null); do
        filename=$(basename "$f")
        echo "Uploading: $filename"
        curl -T "$f" "ftp://$FTP_USER:$FTP_PASSWORD@$FTP_HOST$FTP_DIR/$filename" -s -f
        echo "  -> OK"
    done
done

echo ""
echo "Deployment complete!"
echo "URL: https://$FTP_HOST/"

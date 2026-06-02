#!/usr/bin/env bash
# build_release.sh — 构建 Release tarball
# 用法：./scripts/build_release.sh [version]
# 输出：dist/zoom-monitor-<version>.tar.gz

set -euo pipefail

VERSION="${1:-v1.0.0-lite}"
BASE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DIST_DIR="$BASE_DIR/dist"
RELEASE_NAME="zoom-monitor-$VERSION"
RELEASE_DIR="/tmp/$RELEASE_NAME"

echo "Building release: $VERSION"

# Clean
rm -rf "$RELEASE_DIR" "$DIST_DIR"
mkdir -p "$RELEASE_DIR" "$DIST_DIR"

# Copy release files
echo "Copying files..."
cp "$BASE_DIR/README.md" "$RELEASE_DIR/"
cp "$BASE_DIR/VERSION" "$RELEASE_DIR/"
cp "$BASE_DIR/CHANGELOG.md" "$RELEASE_DIR/"
cp "$BASE_DIR/.env.example" "$RELEASE_DIR/"
cp "$BASE_DIR/requirements.txt" "$RELEASE_DIR/"
cp "$BASE_DIR/brand.json" "$RELEASE_DIR/"
cp "$BASE_DIR/.gitignore" "$RELEASE_DIR/"
cp "$BASE_DIR/install.sh" "$RELEASE_DIR/"
cp -r "$BASE_DIR/docs" "$RELEASE_DIR/"
cp -r "$BASE_DIR/scripts" "$RELEASE_DIR/"
cp -r "$BASE_DIR/systemd" "$RELEASE_DIR/"

# Python 源码（不含 __pycache__）
for f in "$BASE_DIR"/*.py; do
    cp "$f" "$RELEASE_DIR/"
done

# Template 和静态文件
cp -r "$BASE_DIR/templates" "$RELEASE_DIR/"
cp -r "$BASE_DIR/static" "$RELEASE_DIR/"

# Docker compose
cp "$BASE_DIR/docker-compose.yml" "$RELEASE_DIR/" 2>/dev/null || true
cp "$BASE_DIR/Dockerfile" "$RELEASE_DIR/" 2>/dev/null || true
cp "$BASE_DIR/.dockerignore" "$RELEASE_DIR/" 2>/dev/null || true

# 清理 pycache
find "$RELEASE_DIR" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true

# 创建 tarball
tar -C /tmp -czf "$DIST_DIR/$RELEASE_NAME.tar.gz" "$RELEASE_NAME"

echo ""
echo "Release package created:"
echo "  $DIST_DIR/$RELEASE_NAME.tar.gz"
echo ""
echo "File list:"
tar -tzf "$DIST_DIR/$RELEASE_NAME.tar.gz" | head -30
echo "..."
tar -tzf "$DIST_DIR/$RELEASE_NAME.tar.gz" | wc -l | xargs echo "Total files:"

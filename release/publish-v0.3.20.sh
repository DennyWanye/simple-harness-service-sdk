set -euo pipefail

# Run manually from the Service SDK repository root; verify the frozen bytes first.
(cd dist/candidate-0.3.20 && shasum -a 256 -c SHA256SUMS)

# Tag the source commit recorded in BUILD_INFO.json and candidate-manifest.json.
git tag -a v0.3.20 -m "simple-harness-service-sdk v0.3.20" f34caa3c82f1da73df0ea7bdf91f055a7c179f35
# Publish the main branch and annotated tag only after manual release approval.
git push origin main v0.3.20
# Upload the existing candidate assets without rebuilding them.
gh release create v0.3.20 --title "simple-harness-service-sdk v0.3.20" --notes-file release/notes-v0.3.20.md dist/candidate-0.3.20/*

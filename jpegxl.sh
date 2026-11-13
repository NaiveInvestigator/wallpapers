#!/usr/bin/env bash

set -uo pipefail

INPUT_DIR="original"
OUTPUT_DIR="jxl"
DRY_RUN=0

usage() {
    echo "Usage: $0 [-i input_dir] [-o output_dir] [--dry-run]"
    exit 1
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -i|--input)
            INPUT_DIR="$2"
            shift 2
            ;;
        -o|--output)
            OUTPUT_DIR="$2"
            shift 2
            ;;
        --dry-run)
            DRY_RUN=1
            shift
            ;;
        *)
            usage
            ;;
    esac
done

if ! command -v cjxl >/dev/null; then
    echo "Error: cjxl not installed."
    exit 1
fi

mkdir -p "$OUTPUT_DIR"

mapfile -t FILES < <(
    find "$INPUT_DIR" -type f \( \
        -iname "*.png" \
        -o -iname "*.jpg" \
        -o -iname "*.jpeg" \
    \) | sort
)

TOTAL=${#FILES[@]}

if [[ $TOTAL -eq 0 ]]; then
    echo "No images found."
    exit 0
fi

PNG_COUNT=0
JPG_COUNT=0

for f in "${FILES[@]}"; do
    case "${f,,}" in
        *.png)  PNG_COUNT=$((PNG_COUNT+1)) ;;
        *.jpg|*.jpeg) JPG_COUNT=$((JPG_COUNT+1)) ;;
    esac
done

echo "Input folder : $INPUT_DIR"
echo "Output folder: $OUTPUT_DIR"
echo "Files found  : $TOTAL"
echo
echo "Summary:"
echo "  PNG  → JXL lossless            : $PNG_COUNT"
echo "  JPEG → JXL lossless transcode  : $JPG_COUNT"
echo

if [[ $DRY_RUN -eq 1 ]]; then
    echo "Dry run enabled."
    echo
    for f in "${FILES[@]}"; do
        base=$(basename "${f%.*}")
        out="$OUTPUT_DIR/$base.jxl"

        case "${f,,}" in
            *.png)
                echo "cjxl \"$f\" \"$out\" -d 0 -e 7"
                ;;
            *.jpg|*.jpeg)
                echo "cjxl \"$f\" \"$out\" --lossless_jpeg=1 -e 7"
                ;;
        esac
    done
    exit 0
fi

echo "Starting conversion..."
echo

START_TIME=$(date +%s)
DONE=0
FAILED=0

for f in "${FILES[@]}"; do

    base=$(basename "${f%.*}")
    out="$OUTPUT_DIR/$base.jxl"

    if [[ -f "$out" ]]; then
        ((DONE++))
        continue
    fi

    case "${f,,}" in
        *.png)
            cjxl "$f" "$out" -d 0 -e 9 >/dev/null 2>&1
            status=$?
            ;;
        *.jpg|*.jpeg)
            cjxl "$f" "$out" --lossless_jpeg=1 >/dev/null 2>&1
            status=$?
            ;;
    esac

    if [[ $status -ne 0 ]]; then
        echo
        echo "Failed: $f"
        ((FAILED++))
    fi

    ((DONE++))

    NOW=$(date +%s)
    ELAPSED=$((NOW - START_TIME))
    [[ $ELAPSED -eq 0 ]] && ELAPSED=1

    SPEED=$(awk "BEGIN {printf \"%.2f\", $DONE/$ELAPSED}")
    REMAIN=$((TOTAL - DONE))

    printf "\r[%d/%d] done | %d left | %s img/s | %ds elapsed" \
        "$DONE" "$TOTAL" "$REMAIN" "$SPEED" "$ELAPSED"

done

echo
echo
echo "Finished."
echo "Converted : $DONE"
echo "Failed    : $FAILED"

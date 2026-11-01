---
name: ffmpeg-encoder-check
description: Check FFmpeg encoder availability before video encoding to avoid library mismatches
---

# FFmpeg Encoder Availability Check

## Purpose

Before writing video encoding scripts, always verify which H.264 encoders are available in your FFmpeg installation. This prevents failures from library version mismatches, particularly with libopenh264.

## Instructions

### Step 1: Probe Available Encoders

Run the following command to check available H.264 encoders:

```bash
ffmpeg -encoders 2>/dev/null | grep h264
```

This shows which H.264 encoders are compiled into your FFmpeg build.

### Step 2: Interpret Results

Common encoder options you may see:

| Encoder | Description | Recommendation |
|---------|-------------|----------------|
| `libx264` | Software H.264 encoder | **Preferred** - widely compatible |
| `libopenh264` | OpenH264 software encoder | Use with caution - often has library version mismatches |
| `h264_nvenc` | NVIDIA hardware encoder | Good if NVIDIA GPU available |
| `h264_videotoolbox` | macOS hardware encoder | Good on macOS |
| `h264_vaapi` | Intel VAAPI hardware encoder | Good on Linux with Intel GPU |
| `h264_qsv` | Intel QuickSync encoder | Good on Windows/Linux with Intel GPU |

### Step 3: Choose Encoding Strategy

**For same-resolution sources (no re-encoding needed):**

```bash
# Best option - pass-through without quality loss
ffmpeg -i input.mp4 -c:v copy -c:a copy output.mp4
```

**If libx264 is available:**

```bash
# Reliable software encoding
ffmpeg -i input.mp4 -c:v libx264 -preset medium -crf 23 -c:a aac output.mp4
```

**If only libopenh264 is available:**

```bash
# Use with caution - may have library issues
ffmpeg -i input.mp4 -c:v libopenh264 -c:a aac output.mp4
```

### Step 4: Validate Before Batch Processing

Always test your encoding command on a small sample file before processing multiple videos or long footage.

## Best Practices

1. **Default to `-c:v copy`** when source and target resolutions match - no quality loss, fastest processing
2. **Prefer libx264** over libopenh264 for software encoding - more stable, better compatibility
3. **Check encoder availability at script startup**, not during execution - fail fast with clear error
4. **Cache encoder check results** if running multiple encoding operations in the same session
5. **Provide fallback options** in automated scripts - try copy first, then libx264, then fail gracefully

## Example Script Template

```bash
#!/bin/bash

# Check available encoders at startup
ENCODERS=$(ffmpeg -encoders 2>/dev/null | grep h264)

if echo "$ENCODERS" | grep -q "libx264"; then
    VIDEO_CODEC="libx264"
    echo "Using libx264 encoder"
elif echo "$ENCODERS" | grep -q "libopenh264"; then
    VIDEO_CODEC="libopenh264"
    echo "Warning: Using libopenh264 (may have compatibility issues)"
else
    echo "Error: No H.264 encoder available"
    echo "Available encoders:"
    echo "$ENCODERS"
    exit 1
fi

# For same-resolution sources, prefer copy
if [ "$SOURCE_RESOLUTION" = "$TARGET_RESOLUTION" ]; then
    VIDEO_CODEC="copy"
    echo "Same resolution detected - using stream copy"
fi

# Encode
ffmpeg -i "$INPUT" -c:v "$VIDEO_CODEC" -c:a aac "$OUTPUT"
```

## Common Errors to Avoid

- **Do not assume libx264 is available** - FFmpeg builds vary by system
- **Do not use libopenh264 without checking** - frequent library version mismatch errors
- **Do not re-encode unnecessarily** - use `-c:v copy` when resolution matches
- **Do not skip the encoder check** - always probe before writing encoding logic
The patch comprehensively addresses all identified issues:

1. **Added fallback strategies** for when Python libraries (librosa, soundfile, python-docx) are unavailable
2. **Documented command-line alternatives**: 
   - DOCX parsing with `unzip -p file.docx word/document.xml | sed` and `pandoc`
   - Audio analysis with `ffprobe` and `sox` instead of librosa
3. **Added troubleshooting section** for common import failures (librosa, soundfile, libsndfile)
4. **Added Tool Selection Matrix** showing preferred tools and fallbacks for each task
5. **Enhanced verification steps** with CLI-only methods using ffprobe/sox

This directly addresses the execution failure context where the agent successfully completed the task using shell command-line tools despite Python library failures.

### Tool Availability Fallback Strategy

This workflow is designed to handle environments where Python libraries may be unavailable. Always attempt the primary Python-based approach first, but be prepared to fall back to command-line alternatives:

| Task | Primary Tool | Fallback Option 1 | Fallback Option 2 |
|------|-------------|-------------------|-------------------|
| DOCX Parsing | `python-docx` | `pandoc --to plain` | `unzip -p file.docx word/document.xml \| sed` |
| Audio Analysis | `librosa` | `ffprobe` + `sox` | `ffprobe` only |
| Audio Editing | `pydub` + `librosa` | `sox` + `ffmpeg` | `ffmpeg` only |
| Format Verification | `librosa` | `ffprobe` | `sox --info` |
### Step 0: Tool Detection and Fallback Planning

Before beginning the main workflow, verify tool availability:

```bash
# Check Python libraries
python3 -c "import librosa" 2>/dev/null && echo "librosa: OK" || echo "librosa: MISSING"
python3 -c "import soundfile" 2>/dev/null && echo "soundfile: OK" || echo "soundfile: MISSING"
python3 -c "import docx" 2>/dev/null && echo "python-docx: OK" || echo "python-docx: MISSING"

# Check CLI alternatives
which ffprobe && echo "ffprobe: OK" || echo "ffprobe: MISSING"
which sox && echo "sox: OK" || echo "sox: MISSING"
which pandoc && echo "pandoc: OK" || echo "pandoc: MISSING"
which unzip && echo "unzip: OK" || echo "unzip: MISSING"
```

Document which tools are available and plan your approach accordingly.
@@ 
**Fallback: DOCX parsing with command-line tools**

If `python-docx` is unavailable, use these alternatives:

**Option A: Using pandoc (if available)**
```bash
pandoc --to plain project_notes.docx | grep -E "[0-9]{2}:[0-9]{2}:[0-9]{2}"
```

**Option B: Using unzip + sed (universal fallback)**
DOCX files are ZIP archives containing XML. Extract and parse directly:
```bash
# Extract raw XML from DOCX and search for timecodes
unzip -p project_notes.docx word/document.xml | \
  sed 's/<[^>]*>//g' | \
  grep -oE "[0-9]{2}:[0-9]{2}:[0-9]{2}[,\.][0-9]{2}" | \
  sort -u
```
@@ 
**Fallback: Audio analysis with ffprobe and sox**

If Python audio libraries fail, use CLI tools:

**Option A: Using ffprobe (recommended)**
```bash
# Get comprehensive audio info
ffprobe -v error -select_streams a:0 -show_entries stream=codec_name,sample_rate,channels,bits_per_sample,duration \
  -of csv=p=0 input.wav

# Get duration in seconds (precise)
ffprobe -v error -show_entries format=duration -of csv=p=0 input.wav

# Get sample rate
ffprobe -v error -select_streams a:0 -show_entries stream=sample_rate -of csv=p=0 input.wav
```

**Option B: Using sox**
```bash
# Get full audio info
sox --info input.wav 2>&1

# Parse specific values
sox --info input.wav 2>&1 | grep "Sample Rate" | awk '{print $NF}'
sox --info input.wav 2>&1 | grep "Channels" | awk '{print $NF}'
```
@@ 
**Fallback: Analysis with sox and ffmpeg**

If librosa is unavailable:

```bash
# Generate RMS/amplitude stats per segment
sox input.wav -n stats 2>&1 | grep "RMS amplitude"

# Find zero crossings for clean edit points
sox input.wav -n noops stat 2>&1 | grep "Zero crossings"

# Generate spectral analysis with ffmpeg
ffmpeg -i input.wav -af astats=metadata=1:reset=1 -f null - 2>&1 | grep -E "RMS_level|Peak_level"

# Peak detection using sox
sox input.wav -n stat 2>&1 | grep "Maximum amplitude"
```
@@ 
**Fallback: Audio editing with ffmpeg and sox**

If Python libraries are unavailable:

```bash
# Crossfade between two files (3-second crossfade)
ffmpeg -i part1.wav -i part2.wav -filter_complex \
  "[0:a][1:a]crossfade=d=3:c1=tri:c2=tri[out]" -map "[out]" output.wav

# Cut segment at specific timecodes
ffmpeg -i input.wav -ss 00:00:30.500 -to 00:01:45.200 -c copy segment.wav

# Using sox for simple edits
sox input.wav output.wav trim 30.5 74.7
sox file1.wav file2.wav newfile.wav splice 30,3
sox input.wav output.wav gain -3
```
@@ 
**Fallback: Verification with ffprobe**

```bash
# Complete verification script
verify_audio() {
    local file="$1"
    local expected_sr="$2"
    
    echo "=== Verifying: $file ==="
    
    # Verify sample rate
    SR=$(ffprobe -v error -select_streams a:0 -show_entries stream=sample_rate -of csv=p=0 "$file")
    if [ "$SR" = "$expected_sr" ]; then
        echo "✓ Sample rate: ${SR}Hz"
    else
        echo "✗ Sample rate mismatch: expected $expected_sr, got $SR"
        return 1
    fi
    
    # Verify duration
    DURATION=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$file")
    echo "Duration: ${DURATION}s"
    
    # Verify file exists
    if [ -s "$file" ]; then
        echo "✓ File exists and is non-empty"
    else
        echo "✗ File missing or empty"
        return 1
    fi
}

# Usage: verify_audio "output.wav" "48000"
```
@@ 
### Troubleshooting: Common Import Failures

**Error: `ModuleNotFoundError: No module named 'librosa'`**

```bash
# Immediate fallback: Use ffprobe/sox instead
which ffprobe || which sox || echo "ERROR: No audio tools available"

# If installation is possible:
pip install librosa || pip3 install librosa
```

**Error: `ModuleNotFoundError: No module named 'soundfile'`**

```bash
# soundfile requires libsndfile system library
# Install system dependency first (Ubuntu/Debian):
sudo apt-get install libsndfile1
pip install soundfile

# Fallback: Use ffprobe for all audio I/O metadata
```

**Error: `ImportError: No module named 'docx'` (python-docx)**

```bash
# Install if possible:
pip install python-docx

# Fallback: Use unzip + sed approach (always available):
unzip -p file.docx word/document.xml | sed 's/<[^>]*>//g' > extracted_text.txt

# Or use pandoc if available:
pandoc --to plain file.docx
```

**Error: Sandbox execution failures with audio libraries**

When `execute_code_sandbox` fails repeatedly:
1. Switch to `shell_agent` for direct CLI tool execution
2. Write Python scripts to temporary files and execute via shell
3. Use `ffmpeg` and `sox` which are often pre-installed in minimal environments

```bash
# Pattern: Write script to file, then execute
cat > /tmp/audio_work.py << 'EOF'
# Your Python code here
EOF
python3 /tmp/audio_work.py

# If that fails, use pure shell:
ffprobe -v error -show_entries format=duration -of csv=p=0 file.wav
```

### Tool Selection Matrix Summary

| Environment State | Recommended Approach |
|------------------|---------------------|
| Full Python stack available | Use librosa + python-docx + pydub |
| Python available, no audio libs | Use ffprobe/sox via subprocess |
| Minimal environment (shell only) | Pure CLI: ffprobe, sox, ffmpeg, unzip/sed |
| DOCX parsing fails | Try: python-docx → pandoc → unzip+sed |
| Audio analysis fails | Try: librosa → ffprobe → sox |
| Audio editing fails | Try: pydub → ffmpeg filter_complex → sox |

**Key Principle:** Always have at least two approaches for each task. Test tool availability early and adapt your workflow accordingly.

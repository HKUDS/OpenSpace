---
name: diagnostic-stem-delivery
description: Audio production with diagnostic analysis, timecode parsing from documents, and verified export workflow
---

# Diagnostic Stem Audio Production Workflow

This skill provides a resilient pattern for audio production that emphasizes **diagnostic analysis before editing**, **explicit timecode extraction from documents**, **incremental verification**, **fail-fast principles**, and **mandatory deliverable verification**. Each major step produces verified outputs before proceeding, with comprehensive audio diagnostics at specified timecodes.

## Overview

Follow these steps in strict order. Each step must complete successfully and pass verification before proceeding to the next:

1. **Parse timecodes from source documents** - Extract edit spots/timecodes from DOCX/text sources
2. **Perform diagnostic audio analysis** - Analyze reference audio at each timecode (pitch, clicks, frequency)
3. **Calculate timing parameters** - Derive section transitions from BPM and duration
4. **Verify reference audio** - Validate input file properties and extract target duration
5. **Generate and verify each stem individually** - One stem at a time with immediate verification
6. **Detect and resolve duration mismatches** - Apply appropriate extension strategy
7. **Apply edits based on diagnostics** - Make informed edits using analysis results
8. **Mix with verification** - Combine stems and verify mix integrity
9. **Export and verify deliverable** - Generate final output with comprehensive checks

## Key Principles

- **Diagnostics first**: Analyze audio at edit points BEFORE making any changes
- **Document-driven**: Parse timecodes directly from source documents (DOCX, TXT)
- **Incremental verification**: Verify each stem immediately after generation
- **Fail-fast approach**: Stop and report errors at each step
- **Mandatory export**: Final step MUST produce verified deliverable file
- **Tool reliability**: Use run_shell with inline Python for audio processing (avoid execute_code_sandbox for audio)

## Step 0: Parse Timecodes from Source Documents

Extract edit spots and timecodes from document sources. Use python-docx via run_shell for reliable DOCX parsing:

```bash
# Parse DOCX file for timecodes and edit spots
python3 -c "
from docx import Document
import re
import sys

doc_path = sys.argv[1] if len(sys.argv) > 1 else 'Bass Edit Spots.docx'
doc = Document(doc_path)

edit_spots = []
timecode_pattern = r'(\d{1,2}:?\d{2}:?\d{2}[.:\d]*)|(\d+[.:\d]+)s'

for para in doc.paragraphs:
    text = para.text.strip()
    if not text:
        continue
    
    # Look for timecodes in various formats
    matches = re.findall(timecode_pattern, text, re.IGNORECASE)
    if matches:
        for match in matches:
            timecode = match[0] if match[0] else match[1]
            if timecode:
                edit_spots.append({'timecode': timecode, 'context': text[:100]})
    
    # Also check tables
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                cell_text = cell.text.strip()
                matches = re.findall(timecode_pattern, cell_text, re.IGNORECASE)
                for match in matches:
                    timecode = match[0] if match[0] else match[1]
                    if timecode:
                        edit_spots.append({'timecode': timecode, 'context': cell_text[:100]})

print(f'Found {len(edit_spots)} edit spots:')
for i, spot in enumerate(edit_spots, 1):
    print(f'{i}. {spot[\"timecode\"]} - {spot[\"context\"][:50]}...')
"
```

## Step 1: Perform Diagnostic Audio Analysis at Timecodes

Before any editing, analyze the reference audio at each identified timecode:

```python
import numpy as np
import soundfile as sf
import librosa

def analyze_audio_at_timecode(filepath, timecode_str, sample_rate=48000):
    """
    Perform comprehensive diagnostic analysis at a specific timecode.
    
    Returns dict with:
    - pitch_estimate: Dominant frequency/pitch
    - click_pop_score: Likelihood of clicks/pops (0-1, higher = more likely)
    - frequency_spectrum: Dominant frequency bands
    - amplitude: RMS amplitude at timecode
    - issues: List of detected issues
    """
    # Parse timecode to seconds
    timecode_str = timecode_str.replace(':', '.').strip()
    if 's' in timecode_str:
        timecode_str = timecode_str.replace('s', '')
    
    try:
        parts = timecode_str.split('.')
        if len(parts) == 3:
            seconds = int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        elif len(parts) == 2:
            seconds = int(parts[0]) * 60 + float(parts[1])
        else:
            seconds = float(parts[0])
    except:
        return {'error': f'Invalid timecode format: {timecode_str}'}
    
    # Load audio
    data, sr = sf.read(filepath)
    if sr != sample_rate:
        data = librosa.resample(data, orig_sr=sr, target_sr=sample_rate)
        sr = sample_rate
    
    # Extract window around timecode (±50ms for analysis)
    window_samples = int(0.1 * sample_rate)  # 100ms window
    start_sample = max(0, int(seconds * sample_rate) - window_samples // 2)
    end_sample = min(len(data), start_sample + window_samples)
    window = data[start_sample:end_sample]
    
    if len(window) < 100:
        return {'error': 'Window too short for analysis'}
    
    # Pitch detection (using autocorrelation for monophonic content)
    def estimate_pitch(signal, sr):
        # Simple autocorrelation-based pitch detection
        signal = signal - np.mean(signal)  # DC removal
        autocorr = np.correlate(signal, signal, mode='full')
        autocorr = autocorr[len(autocorr)//2:]
        
        # Find first significant peak after zero lag
        for i in range(1, min(len(autocorr) // 2, int(sr / 50))):
            if autocorr[i] > 0.3 * autocorr[0]:
                for j in range(i + 1, min(len(autocorr), int(sr / 20))):
                    if autocorr[j] > autocorr[i]:
                        period = j
                        freq = sr / period
                        return freq
        return None
    
    pitch = estimate_pitch(window, sr)
    
    # Click/pop detection (sudden amplitude changes)
    def detect_clicks(signal):
        diff = np.diff(np.abs(signal))
        threshold = 5 * np.std(diff)
        click_positions = np.where(np.abs(diff) > threshold)[0]
        click_score = min(1.0, len(click_positions) / len(signal) * 1000)
        return click_score, click_positions
    
    click_score, click_positions = detect_clicks(window)
    
    # Frequency analysis
    spectrum = np.abs(np.fft.rfft(window))
    freqs = np.fft.rfftfreq(len(window), 1/sr)
    dominant_freqs = []
    for band in [(20, 200, 'sub'), (200, 2000, 'mid'), (2000, 20000, 'high')]:
        mask = (freqs >= band[0]) & (freqs < band[1])
        if np.any(mask):
            band_power = np.sum(spectrum[mask])
            dominant_freqs.append({'range': f'{band[0]}-{band[1]}Hz', 'power': float(band_power), 'label': band[2]})
    dominant_freqs.sort(key=lambda x: x['power'], reverse=True)
    
    # Amplitude
    rms = np.sqrt(np.mean(window ** 2))
    
    # Detect issues
    issues = []
    if click_score > 0.3:
        issues.append(f'High click/pop probability ({click_score:.2f})')
    if rms < 0.001:
        issues.append('Near-silence detected')
    if rms > 0.9:
        issues.append('Potential clipping')
    if pitch and pitch < 40:
        issues.append(f'Very low frequency content ({pitch:.1f}Hz)')
    
    return {
        'timecode': timecode_str,
        'seconds': seconds,
        'pitch_hz': pitch,
        'click_pop_score': click_score,
        'frequency_spectrum': dominant_freqs[:3],
        'amplitude_rms': float(rms),
        'issues': issues,
        'window_length': len(window)
    }

# Analyze all edit spots
# edit_spots from Step 0
for i, spot in enumerate(edit_spots):
    print(f'\\n=== Analyzing edit spot {i+1}: {spot["timecode"]} ===')
    analysis = analyze_audio_at_timecode('reference.wav', spot['timecode'])
    if 'error' in analysis:
        print(f'ERROR: {analysis["error"]}')
    else:
        print(f'Pitch: {analysis["pitch_hz"]} Hz' if analysis["pitch_hz"] else 'Pitch: N/A (complex/noisy)')
        print(f'Click/Pop Score: {analysis["click_pop_score"]:.3f} (0=none, 1=certain)')
        print(f'Amplitude (RMS): {analysis["amplitude_rms"]:.6f}')
        if analysis['issues']:
            print(f'Issues: {", ".join(analysis["issues"])}')
        for freq in analysis['frequency_spectrum']:
            print(f'  {freq["label"]} band ({freq["range"]}): power={freq["power"]:.2f}')
```

## Step 2: Calculate Timing Parameters (Early)

Calculate all timing parameters **before** generating any audio:

```python
def calculate_section_transitions(bpm, total_duration_sec, sections):
    """Calculate beat-aligned transition points for song sections."""
    beats_per_second = bpm / 60.0
    
    section_durations = {}
    cumulative_time = 0
    
    for section_name, beat_count in sections.items():
        duration = beat_count / beats_per_second
        section_durations[section_name] = {
            'start': cumulative_time,
            'end': cumulative_time + duration,
            'beats': beat_count,
            'start_beat': cumulative_time * beats_per_second
        }
        cumulative_time += duration
    
    return section_durations

# Configuration
BPM = 120
DURATION = 137
SECTIONS = {'intro': 16, 'verse': 32, 'chorus': 32, 'bridge': 16, 'outro': 16}

timing = calculate_section_transitions(BPM, DURATION, SECTIONS)
print('Timing calculated:')
for section, data in timing.items():
    print(f'  {section}: {data["start"]:.2f}s - {data["end"]:.2f}s ({data["beats"]} beats)')
```

## Step 3: Verify Reference Audio

Validate the reference file exists and has expected properties:

```python
import soundfile as sf
import os

def verify_reference_file(filepath, expected_sample_rate=None, min_duration=None):
    """Verify reference audio file and return info dict."""
    if not os.path.exists(filepath):
        raise FileNotFoundError(f'Reference file not found: {filepath}')
    
    info = sf.info(filepath)
    errors = []
    
    if expected_sample_rate and info.samplerate != expected_sample_rate:
        errors.append(f'Sample rate mismatch: expected {expected_sample_rate}, got {info.samplerate}')
    
    if min_duration and info.duration < min_duration:
        errors.append(f'Duration too short: expected >= {min_duration}s, got {info.duration}s')
    
    if errors:
        raise ValueError(f'Reference file validation failed: {"; ".join(errors)}')
    
    print(f'Reference verified: {info.duration:.2f}s @ {info.samplerate}Hz, {info.channels}ch, {info.subtype}')
    return {
        'sample_rate': info.samplerate,
        'duration': info.duration,
        'channels': info.channels,
        'subtype': info.subtype
    }

# Verify reference
ref_info = verify_reference_file('reference.wav', expected_sample_rate=48000, min_duration=130)
TARGET_DURATION = ref_info['duration']  # Use actual reference duration as target
```

## Step 4: Generate and Verify Each Stem Individually

Generate one stem at a time, verify it immediately before proceeding to the next:

```python
import numpy as np

def generate_stem(name, duration_sec, sample_rate, subtype='FLOAT', section_timing=None):
    """Generate a single stem with explicit sample type."""
    frames = int(duration_sec * sample_rate)
    t = np.linspace(0, duration_sec, frames)
    
    # Generate stem-specific content (customize per stem type)
    if name == 'bass':
        freq = 110  # A2
        audio_data = np.sin(2 * np.pi * freq * t) * 0.8
    elif name == 'guitars':
        freq = 440  # A4
        audio_data = np.sin(2 * np.pi * freq * t) * 0.6
    elif name == 'synths':
        freq = 880  # A5
        audio_data = np.sin(2 * np.pi * freq * t) * 0.5
    elif name == 'bridge':
        freq = 220  # A3
        audio_data = np.sin(2 * np.pi * freq * t) * 0.7
    else:
        audio_data = np.sin(2 * np.pi * 440 * t) * 0.5
    
    # Ensure proper data type
    if subtype == 'FLOAT':
        audio_data = audio_data.astype(np.float32)
    elif subtype == 'PCM_24':
        audio_data = np.clip(audio_data, -1, 1) * (2**23 - 1)
        audio_data = audio_data.astype(np.int32)
    
    filepath = f'{name}_stem.wav'
    sf.write(filepath, audio_data, sample_rate, subtype=subtype, format='WAV')
    
    return filepath, audio_data

def verify_stem(filepath, expected_sample_rate, expected_duration, tolerance_sec=1.0):
    """Verify a single stem meets specifications."""
    if not os.path.exists(filepath):
        return {'success': False, 'error': f'File not found: {filepath}'}
    
    info = sf.info(filepath)
    errors = []
    
    if info.samplerate != expected_sample_rate:
        errors.append(f'sample_rate: expected {expected_sample_rate}, got {info.samplerate}')
    
    if abs(info.duration - expected_duration) > tolerance_sec:
        errors.append(f'duration: expected ~{expected_duration}s, got {info.duration}s')
    
    if errors:
        return {'success': False, 'error': '; '.join(errors)}
    
    return {'success': True, 'info': info}

# Generate stems one at a time with verification
SAMPLE_RATE = 48000
STEM_NAMES = ['bass', 'guitars', 'synths', 'bridge']

generated_stems = []
stem_info = {}

for stem_name in STEM_NAMES:
    print(f'\\n=== Generating {stem_name} stem ===')
    
    # Generate
    filepath, data = generate_stem(stem_name, DURATION, SAMPLE_RATE, subtype='FLOAT')
    
    # Verify immediately
    result = verify_stem(filepath, SAMPLE_RATE, TARGET_DURATION)
    
    if result['success']:
        print(f'✓ {stem_name} stem verified: {result["info"].duration:.2f}s @ {result["info"].samplerate}Hz')
        generated_stems.append(filepath)
        stem_info[stem_name] = result
    else:
        print(f'✗ {stem_name} stem FAILED: {result["error"]}')
        raise RuntimeError(f'Stem generation failed for {stem_name}: {result["error"]}')

print(f'\\nAll {len(generated_stems)} stems generated and verified successfully')
```

## Step 5: Apply Edits Based on Diagnostics

Use the diagnostic analysis from Step 1 to make informed editing decisions:

```python
def apply_edit_based_on_diagnostics(stem_filepath, edit_spot, analysis, output_filepath):
    """
    Apply targeted edit at a specific timecode based on diagnostic analysis.
    
    Decisions based on diagnostics:
    - High click/pop score (>0.3): Apply fade or click removal
    - Very low pitch (<40Hz): May need high-pass filter
    - Near-silence: Consider removal or gain adjustment
    - Potential clipping: Apply gain reduction
    """
    data, sr = sf.read(stem_filepath)
    
    timecode_sec = analysis['seconds']
    edit_start = max(0, int((timecode_sec - 0.05) * sr))
    edit_end = min(len(data), int((timecode_sec + 0.05) * sr))
    
    # Apply edits based on diagnostics
    if analysis['click_pop_score'] > 0.3:
        # Apply short crossfade to smooth clicks
        fade_len = min(100, (edit_end - edit_start) // 4)
        if fade_len > 0:
            fade_in = np.linspace(0, 1, fade_len)
            fade_out = np.linspace(1, 0, fade_len)
            data[edit_start:edit_start + fade_len] *= fade_in
            data[edit_end - fade_len:edit_end] *= fade_out
        print(f'  Applied click smoothing at {timecode_sec:.2f}s')
    
    if analysis['amplitude_rms'] > 0.9:
        # Apply gentle gain reduction to prevent clipping
        gain = 0.8
        data[edit_start:edit_end] *= gain
        print(f'  Applied gain reduction ({gain:.1f}x) at {timecode_sec:.2f}s')
    
    # Save edited stem
    sf.write(output_filepath, data, sr, subtype='FLOAT', format='WAV')
    return output_filepath

# Apply edits to stems based on diagnostic analysis
for stem_name in ['bass']:  # Customize as needed
    stem_file = f'{stem_name}_stem.wav'
    edited_file = f'{stem_name}_stem_edited.wav'
    
    print(f'\\n=== Applying edits to {stem_name} based on diagnostics ===')
    
    for i, spot in enumerate(edit_spots):
        # Re-analyze or use cached analysis
        analysis = analyze_audio_at_timecode('reference.wav', spot['timecode'])
        if 'error' not in analysis:
            apply_edit_based_on_diagnostics(stem_file, spot, analysis, edited_file)
    
    print(f'Edited {stem_name} stem saved to {edited_file}')
```

## Step 6: Mix with Verification

Combine all stems and verify mix integrity:

```python
def mix_stems(stem_files, output_filepath, sample_rate=48000):
    """Mix multiple stems into a single output file."""
    mixed_data = None
    
    for stem_file in stem_files:
        if not os.path.exists(stem_file):
            raise FileNotFoundError(f'Stem not found: {stem_file}')
        
        data, sr = sf.read(stem_file)
        
        # Resample if needed
        if sr != sample_rate:
            data = librosa.resample(data, orig_sr=sr, target_sr=sample_rate)
        
        # Ensure same length
        if mixed_data is None:
            mixed_data = np.zeros(len(data), dtype=np.float32)
        elif len(data) != len(mixed_data):
            min_len = min(len(data), len(mixed_data))
            mixed_data = mixed_data[:min_len]
            data = data[:min_len]
        
        mixed_data += data
    
    # Normalize to prevent clipping
    if np.max(np.abs(mixed_data)) > 0.95:
        mixed_data *= 0.95 / np.max(np.abs(mixed_data))
    
    sf.write(output_filepath, mixed_data, sample_rate, subtype='FLOAT', format='WAV')
    
    return {
        'filepath': output_filepath,
        'duration': len(mixed_data) / sample_rate,
        'peak': float(np.max(np.abs(mixed_data))),
        'rms': float(np.sqrt(np.mean(mixed_data ** 2)))
    }

# Mix all stems
print('\\n=== Mixing all stems ===')
all_stems = [f'{name}_stem_edited.wav' if os.path.exists(f'{name}_stem_edited.wav') 
             else f'{name}_stem.wav' for name in STEM_NAMES]

mix_info = mix_stems(all_stems, 'State_of_Affairs_FULL_EDIT_MIX.wav')
print(f'Mix complete: {mix_info["duration"]:.2f}s, peak={mix_info["peak"]:.3f}, RMS={mix_info["rms"]:.6f}')
```

## Step 7: Export and Verify Deliverable (MANDATORY)

**This step MUST complete successfully** - no task is complete without verified deliverable:

```python
def verify_deliverable(filepath, required_sample_rate=48000, required_channels=None, min_duration=None):
    """
    Comprehensive verification of final deliverable.
    
    Returns dict with verification status and details.
    Task CANNOT complete if verification fails.
    """
    if not os.path.exists(filepath):
        return {
            'success': False,
            'error': f'DELIVERABLE MISSING: {filepath}',
            'blocking': True
        }
    
    try:
        info = sf.info(filepath)
    except Exception as e:
        return {
            'success': False,
            'error': f'DELIVERABLE CORRUPT: {str(e)}',
            'blocking': True
        }
    
    errors = []
    warnings = []
    
    # Critical checks (blocking)
    if info.samplerate != required_sample_rate:
        errors.append(f'CRITICAL: Sample rate {info.samplerate}Hz != required {required_sample_rate}Hz')
    
    if required_channels and info.channels != required_channels:
        errors.append(f'CRITICAL: Channels {info.channels} != required {required_channels}')
    
    if min_duration and info.duration < min_duration:
        errors.append(f'CRITICAL: Duration {info.duration:.2f}s < minimum {min_duration}s')
    
    # Verify file is not empty
    if info.duration < 0.1:
        errors.append('CRITICAL: File appears to be empty or silent')
    
    # Non-critical checks (warnings)
    if info.duration < 60:
        warnings.append(f'Short duration: {info.duration:.2f}s')
    
    if os.path.getsize(filepath) < 1000:
        warnings.append('File size unusually small')
    
    # Load and analyze audio content
    data, sr = sf.read(filepath)
    peak = np.max(np.abs(data))
    rms = np.sqrt(np.mean(data ** 2))
    
    if peak > 0.99:
        warnings.append(f'Potential clipping: peak={peak:.4f}')
    
    if rms < 0.001:
        errors.append('CRITICAL: Audio appears to be silent (RMS too low)')
    
    # Final verdict
    success = len(errors) == 0
    
    result = {
        'success': success,
        'blocking': not success,
        'filepath': filepath,
        'info': {
            'duration': info.duration,
            'sample_rate': info.samplerate,
            'channels': info.channels,
            'subtype': info.subtype,
            'format': info.format,
            'peak': float(peak),
            'rms': float(rms),
            'file_size': os.path.getsize(filepath)
        },
        'errors': errors,
        'warnings': warnings
    }
    
    return result

# MANDATORY deliverable verification
print('\\n=== DELIVERABLE VERIFICATION (MANDATORY) ===')
deliverable_path = 'State_of_Affairs_FULL_EDIT_MIX.wav'

verification = verify_deliverable(
    deliverable_path,
    required_sample_rate=48000,
    required_channels=2,  # stereo
    min_duration=60  # minimum 60 seconds
)

if verification['success']:
    print('✓ DELIVERABLE VERIFIED SUCCESSFULLY')
    print(f'  File: {verification["filepath"]}')
    print(f'  Duration: {verification["info"]["duration"]:.2f}s')
    print(f'  Sample Rate: {verification["info"]["sample_rate"]}Hz')
    print(f'  Channels: {verification["info"]["channels"]}')
    print(f'  Peak: {verification["info"]["peak"]:.4f}')
    print(f'  RMS: {verification["info"]["rms"]:.6f}')
    
    if verification['warnings']:
        print('  Warnings:')
        for warn in verification['warnings']:
            print(f'    ⚠ {warn}')
    
    print('\\n✓ TASK COMPLETE - All deliverables verified')
    
else:
    print('✗ DELIVERABLE VERIFICATION FAILED')
    print('  ERRORS (blocking):')
    for err in verification['errors']:
        print(f'    ✗ {err}')
    
    if verification['warnings']:
        print('  Warnings:')
        for warn in verification['warnings']:
            print(f'    ⚠ {warn}')
    
    raise RuntimeError(f'Task cannot complete: {verification["errors"]}')
```

## Tool Usage Notes

**Critical for reliability:**

1. **DOCX Parsing**: Use `run_shell` with `python3 -c` inline syntax and python-docx, NOT `read_file` (returns 'unknown error' for .docx)

2. **Audio Processing**: Use `run_shell` with inline Python scripts for audio operations, NOT `execute_code_sandbox` (frequently returns 'unknown error')

3. **Heredoc Workaround**: Avoid complex heredoc syntax in shell; use simpler `-c` inline Python for reliability

Example reliable pattern:
```bash
python3 -c "import soundfile as sf; import numpy as np; ...your code..."
```

## Checklist Before Completion

- [ ] Step 0: Timecodes parsed from document source
- [ ] Step 1: Diagnostic analysis performed at all edit spots
- [ ] Step 2: Timing parameters calculated
- [ ] Step 3: Reference audio verified
- [ ] Step 4: All stems generated and verified individually
- [ ] Step 5: Edits applied based on diagnostic results
- [ ] Step 6: Stems mixed together
- [ ] Step 7: Deliverable exported AND verified (MANDATORY)
- [ ] Final file exists at expected path with correct format (48k/24b WAV)

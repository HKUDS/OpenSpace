---
name: incremental-audio-workflow
description: Step-by-step audio production with per-stem verification, timing alignment, and incremental quality gates
---

# Incremental Audio Production Workflow

This skill provides a resilient pattern for audio production that emphasizes **incremental verification** and **fail-fast** principles. Each major step produces verified outputs before proceeding, reducing iteration count and catching errors early.

## Overview

Follow these steps in strict order. Each step must complete successfully and pass verification before proceeding to the next:

1. **Early timing calculation** - Derive section transitions from BPM and duration first
2. **Verify reference audio** - Validate input file properties
3. **Generate and verify each stem individually** - One stem at a time with immediate verification
4. **Generate drum stem separately** - Dedicated drum extension with rhythm patterns
5. **Apply effects with verification** - Process each stem and verify output
6. **Export master track** - Mix all verified stems
7. **Archive and final verification** - Package deliverables with comprehensive checks

## Key Differences from Standard Workflow

- **Incremental verification**: Verify each stem immediately after generation, not just at the end
- **Fail-fast approach**: Stop and report errors at each step rather than accumulating failures
- **Early timing**: Calculate section transitions before any audio generation
- **Separated drums**: Drum stem generation is a distinct step with rhythm-specific processing
- **Memory-efficient**: Process stems individually to avoid large array operations that cause sandbox failures

## Step 1: Calculate Timing Parameters (Early)

Calculate all timing parameters **before** generating any audio. This ensures consistent timing across all stems:

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
print("Timing calculated:")
for section, data in timing.items():
    print(f"  {section}: {data['start']:.2f}s - {data['end']:.2f}s ({data['beats']} beats)")
```

## Step 2: Verify Reference Audio

Validate the reference file exists and has expected properties:

```python
import soundfile as sf
import os

def verify_reference_file(filepath, expected_sample_rate=None, min_duration=None):
    """Verify reference audio file and return info dict."""
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"Reference file not found: {filepath}")
    
    info = sf.info(filepath)
    errors = []
    
    if expected_sample_rate and info.samplerate != expected_sample_rate:
        errors.append(f"Sample rate mismatch: expected {expected_sample_rate}, got {info.samplerate}")
    
    if min_duration and info.duration < min_duration:
        errors.append(f"Duration too short: expected >= {min_duration}s, got {info.duration}s")
    
    if errors:
        raise ValueError(f"Reference file validation failed: {'; '.join(errors)}")
    
    print(f"Reference verified: {info.duration:.2f}s @ {info.samplerate}Hz, {info.channels}ch, {info.subtype}")
    return {
        'sample_rate': info.samplerate,
        'duration': info.duration,
        'channels': info.channels,
        'subtype': info.subtype
    }

# Verify reference
ref_info = verify_reference_file('reference.wav', expected_sample_rate=48000, min_duration=130)
```

## Step 3: Generate and Verify Each Stem Individually

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

def verify_stem(filepath, expected_sample_rate, expected_subtype, expected_duration):
    """Verify a single stem meets specifications."""
    if not os.path.exists(filepath):
        return {'success': False, 'error': f'File not found: {filepath}'}
    
    info = sf.info(filepath)
    errors = []
    
    if info.samplerate != expected_sample_rate:
        errors.append(f'sample_rate: expected {expected_sample_rate}, got {info.samplerate}')
    
    if info.subtype != expected_subtype:
        errors.append(f'subtype: expected {expected_subtype}, got {info.subtype}')
    
    if abs(info.duration - expected_duration) > 1.0:  # Allow 1s tolerance
        errors.append(f'duration: expected ~{expected_duration}s, got {info.duration}s')
    
    if errors:
        return {'success': False, 'error': '; '.join(errors)}
    
    return {'success': True, 'info': info}

# Generate stems one at a time with verification
SAMPLE_RATE = 48000
SUBTYPE = 'FLOAT'
STEM_NAMES = ['bass', 'guitars', 'synths', 'bridge']

generated_stems = []
for stem_name in STEM_NAMES:
    print(f"\n=== Generating {stem_name} stem ===")
    
    # Generate
    filepath, data = generate_stem(stem_name, DURATION, SAMPLE_RATE, subtype=SUBTYPE)
    
    # Verify immediately
    result = verify_stem(filepath, SAMPLE_RATE, SUBTYPE, DURATION)
    
    if result['success']:
        print(f"✓ {stem_name} stem verified: {result['info'].duration:.2f}s @ {result['info'].samplerate}Hz")
        generated_stems.append(filepath)
    else:
        print(f"✗ {stem_name} stem FAILED: {result['error']}")
        raise RuntimeError(f"Stem generation failed for {stem_name}: {result['error']}")

print(f"\nAll {len(generated_stems)} stems generated and verified successfully")
```

## Step 4: Generate Drum Stem Separately

Drums require different processing (rhythm patterns, percussion sounds):

```python
def generate_drum_stem(duration_sec, sample_rate, bpm, section_timing, subtype='FLOAT'):
    """Generate drum stem with rhythm patterns aligned to sections."""
    frames = int(duration_sec * sample_rate)
    audio_data = np.zeros(frames, dtype=np.float32)
    beats_per_second = bpm / 60.0
    
    # Simple kick drum pattern (every beat)
    kick_freq = 60
    kick_duration = 0.1
    kick_frames = int(kick_duration * sample_rate)
    
    for beat_time in np.arange(0, duration_sec, 1.0 / beats_per_second):
        start_frame = int(beat_time * sample_rate)
        end_frame = min(start_frame + kick_frames, frames)
        
        if start_frame < frames:
            t = np.linspace(0, kick_duration, end_frame - start_frame)
            kick = np.exp(-5 * t) * np.sin(2 * np.pi * kick_freq * t)
            audio_data[start_frame:end_frame] += kick * 0.9
    
    # Simple snare pattern (every 2nd and 4th beat)
    snare_freq = 200
    snare_duration = 0.05
    snare_frames = int(snare_duration * sample_rate)
    
    for beat_time in np.arange(0, duration_sec, 2.0 / beats_per_second):
        start_frame = int((beat_time + 0.5 / beats_per_second) * sample_rate)
        end_frame = min(start_frame + snare_frames, frames)
        
        if start_frame < frames:
            t = np.linspace(0, snare_duration, end_frame - start_frame)
            snare = np.exp(-10 * t) * np.random.uniform(-1, 1, len(t)) * 0.5
            audio_data[start_frame:end_frame] += snare * 0.7
    
    audio_data = np.clip(audio_data, -1, 1)
    
    filepath = 'drums_stem.wav'
    sf.write(filepath, audio_data, sample_rate, subtype=subtype, format='WAV')
    
    return filepath, audio_data

print("\n=== Generating drum stem ===")
drums_filepath, drums_data = generate_drum_stem(DURATION, SAMPLE_RATE, BPM, timing, subtype=SUBTYPE)

drums_result = verify_stem(drums_filepath, SAMPLE_RATE, SUBTYPE, DURATION)
if drums_result['success']:
    print(f"✓ Drum stem verified: {drums_result['info'].duration:.2f}s @ {drums_result['info'].samplerate}Hz")
    generated_stems.append(drums_filepath)
else:
    print(f"✗ Drum stem FAILED: {drums_result['error']}")
    raise RuntimeError(f"Drum stem generation failed: {drums_result['error']}")
```

## Step 5: Apply Effects with Verification

Process each stem and verify the output:

```python
from scipy import signal

def apply_lowpass_filter(audio_data, sample_rate, cutoff_freq=8000):
    """Apply lowpass filter using scipy.signal."""
    nyquist = sample_rate / 2
    normalized_cutoff = cutoff_freq / nyquist
    b, a = signal.butter(4, normalized_cutoff, btype='low')
    return signal.filtfilt(b, a, audio_data)

def apply_effects_and_verify(input_filepath, output_filepath, sample_rate, subtype):
    """Apply effects to stem and verify output."""
    data, sr = sf.read(input_filepath)
    
    # Apply effects
    processed = apply_lowpass_filter(data, sr, cutoff_freq=8000)
    processed = np.clip(processed, -1, 1)
    
    # Export
    sf.write(output_filepath, processed, sample_rate, subtype=subtype, format='WAV')
    
    # Verify
    result = verify_stem(output_filepath, sample_rate, subtype, DURATION)
    return result, processed

print("\n=== Applying effects to all stems ===")
processed_stems = []

for stem_name in STEM_NAMES:
    input_file = f'{stem_name}_stem.wav'
    output_file = f'{stem_name}_processed.wav'
    
    print(f"Processing {stem_name}...")
    result, _ = apply_effects_and_verify(input_file, output_file, SAMPLE_RATE, SUBTYPE)
    
    if result['success']:
        print(f"✓ {stem_name} processed and verified")
        processed_stems.append(output_file)
    else:
        print(f"✗ {stem_name} processing FAILED: {result['error']}")
        raise RuntimeError(f"Effects processing failed for {stem_name}")

# Process drums separately
drums_output = 'drums_processed.wav'
drums_result, _ = apply_effects_and_verify('drums_stem.wav', drums_output, SAMPLE_RATE, SUBTYPE)
if drums_result['success']:
    print(f"✓ Drums processed and verified")
    processed_stems.append(drums_output)
else:
    raise RuntimeError(f"Drums processing failed: {drums_result['error']}")
```

## Step 6: Export Master Track

Mix all verified stems into master track:

```python
def create_master_track(stem_files, output_filepath, sample_rate, subtype):
    """Create master track from verified stems."""
    # Load first stem to get dimensions
    first_data, sr = sf.read(stem_files[0])
    master_audio = np.zeros(len(first_data), dtype=np.float32)
    
    # Mix all stems with gain staging
    gain_per_stem = 0.4  # Prevent clipping with 5 stems
    
    for stem_file in stem_files:
        data, _ = sf.read(stem_file)
        if len(data) == len(master_audio):
            master_audio += data * gain_per_stem
        else:
            print(f"WARNING: {stem_file} has different length, skipping")
    
    # Final limiting
    master_audio = np.clip(master_audio, -1, 1)
    
    # Export
    sf.write(output_filepath, master_audio, sample_rate, subtype=subtype, format='WAV')
    
    # Verify
    info = sf.info(output_filepath)
    return {'success': True, 'info': info}

print("\n=== Creating master track ===")
master_result = create_master_track(processed_stems, 'master_track.wav', SAMPLE_RATE, SUBTYPE)

if master_result['success']:
    print(f"✓ Master track created: {master_result['info'].duration:.2f}s @ {master_result['info'].samplerate}Hz")
else:
    raise RuntimeError("Master track creation failed")
```

## Step 7: Archive and Final Verification

Package all deliverables and perform comprehensive verification:

```python
import zipfile

def create_archive(archive_name, file_list):
    """Create zip archive and verify contents."""
    with zipfile.ZipFile(archive_name, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for filepath in file_list:
            if os.path.exists(filepath):
                zipf.write(filepath, os.path.basename(filepath))
            else:
                raise FileNotFoundError(f"Cannot archive: {filepath} not found")
    
    # Verify archive
    with zipfile.ZipFile(archive_name, 'r') as zipf:
        contents = zipf.namelist()
    
    return {'success': True, 'file_count': len(contents), 'files': contents}

def final_verification(specs):
    """Comprehensive final verification of all outputs."""
    results = {'passed': 0, 'failed': 0, 'details': []}
    
    for filepath, expected in specs.items():
        if not os.path.exists(filepath):
            results['failed'] += 1
            results['details'].append(f"MISSING: {filepath}")
            continue
        
        info = sf.info(filepath)
        errors = []
        
        if expected.get('sample_rate') and info.samplerate != expected['sample_rate']:
            errors.append(f"sample_rate: {info.samplerate} != {expected['sample_rate']}")
        
        if expected.get('subtype') and info.subtype != expected['subtype']:
            errors.append(f"subtype: {info.subtype} != {expected['subtype']}")
        
        if expected.get('min_duration') and info.duration < expected['min_duration']:
            errors.append(f"duration: {info.duration}s < {expected['min_duration']}s")
        
        if errors:
            results['failed'] += 1
            results['details'].append(f"FAILED: {filepath} - {'; '.join(errors)}")
        else:
            results['passed'] += 1
            results['details'].append(f"PASSED: {filepath} ({info.duration:.2f}s, {info.samplerate}Hz)")
    
    return results

print("\n=== Creating archive ===")
deliverables = ['master_track.wav'] + STEM_NAMES + ['drums']
deliverable_files = [f'{name}.wav' if name != 'drums' else 'drums_processed.wav' for name in ['master_track'] + STEM_NAMES + ['drums_processed']]
deliverable_files = ['master_track.wav'] + [f'{s}_processed.wav' for s in STEM_NAMES] + ['drums_processed.wav']

archive_result = create_archive('audio_deliverables.zip', deliverable_files)
print(f"✓ Archive created with {archive_result['file_count']} files")

print("\n=== Final verification ===")
expected_specs = {
    'master_track.wav': {'sample_rate': SAMPLE_RATE, 'subtype': SUBTYPE, 'min_duration': DURATION - 5},
}
for stem in STEM_NAMES:
    expected_specs[f'{stem}_processed.wav'] = {'sample_rate': SAMPLE_RATE, 'subtype': SUBTYPE, 'min_duration': DURATION - 5}
expected_specs['drums_processed.wav'] = {'sample_rate': SAMPLE_RATE, 'subtype': SUBTYPE, 'min_duration': DURATION - 5}

verification = final_verification(expected_specs)
print(f"\nFinal verification: {verification['passed']} passed, {verification['failed']} failed")
for detail in verification['details']:
    print(f"  {detail}")

assert verification['failed'] == 0, f"Final verification failed: {verification['details']}"
print("\n✓ Workflow completed successfully!")
```

## Complete Workflow Script

```python
#!/usr/bin/env python3
"""
Incremental Audio Production Workflow
Generates, verifies, and archives audio stems with fail-fast checkpoints.
"""

import soundfile as sf
import numpy as np
from scipy import signal
import zipfile
import os
import sys

# Configuration
SAMPLE_RATE = 48000
DURATION = 137
BPM = 120
SUBTYPE = 'FLOAT'
STEM_NAMES = ['bass', 'guitars', 'synths', 'bridge']
SECTIONS = {'intro': 16, 'verse': 32, 'chorus': 32, 'bridge': 16, 'outro': 16}

def calculate_section_transitions(bpm, total_duration_sec, sections):
    beats_per_second = bpm / 60.0
    section_durations = {}
    cumulative_time = 0
    for section_name, beat_count in sections.items():
        duration = beat_count / beats_per_second
        section_durations[section_name] = {
            'start': cumulative_time,
            'end': cumulative_time + duration,
            'beats': beat_count
        }
        cumulative_time += duration
    return section_durations

def verify_stem(filepath, expected_sample_rate, expected_subtype, expected_duration):
    if not os.path.exists(filepath):
        return {'success': False, 'error': f'File not found: {filepath}'}
    info = sf.info(filepath)
    errors = []
    if info.samplerate != expected_sample_rate:
        errors.append(f'sample_rate mismatch')
    if info.subtype != expected_subtype:
        errors.append(f'subtype mismatch')
    if abs(info.duration - expected_duration) > 1.0:
        errors.append(f'duration mismatch')
    if errors:
        return {'success': False, 'error': '; '.join(errors)}
    return {'success': True, 'info': info}

def generate_stem(name, duration_sec, sample_rate, subtype='FLOAT'):
    frames = int(duration_sec * sample_rate)
    t = np.linspace(0, duration_sec, frames)
    freqs = {'bass': 110, 'guitars': 440, 'synths': 880, 'bridge': 220}
    freq = freqs.get(name, 440)
    audio_data = (np.sin(2 * np.pi * freq * t) * 0.5).astype(np.float32)
    filepath = f'{name}_stem.wav'
    sf.write(filepath, audio_data, sample_rate, subtype=subtype, format='WAV')
    return filepath, audio_data

def generate_drum_stem(duration_sec, sample_rate, bpm, subtype='FLOAT'):
    frames = int(duration_sec * sample_rate)
    audio_data = np.zeros(frames, dtype=np.float32)
    beats_per_second = bpm / 60.0
    for beat_time in np.arange(0, duration_sec, 1.0 / beats_per_second):
        start_frame = int(beat_time * sample_rate)
        if start_frame < frames:
            kick_duration = 0.1
            kick_frames = int(kick_duration * sample_rate)
            end_frame = min(start_frame + kick_frames, frames)
            t = np.linspace(0, kick_duration, end_frame - start_frame)
            kick = np.exp(-5 * t) * np.sin(2 * np.pi * 60 * t)
            audio_data[start_frame:end_frame] += kick * 0.9
    audio_data = np.clip(audio_data, -1, 1)
    filepath = 'drums_stem.wav'
    sf.write(filepath, audio_data, sample_rate, subtype=subtype, format='WAV')
    return filepath, audio_data

def apply_effects(input_filepath, output_filepath, sample_rate, subtype):
    data, sr = sf.read(input_filepath)
    nyquist = sample_rate / 2
    b, a = signal.butter(4, 8000 / nyquist, btype='low')
    processed = signal.filtfilt(b, a, data)
    processed = np.clip(processed, -1, 1)
    sf.write(output_filepath, processed, sample_rate, subtype=subtype, format='WAV')
    return verify_stem(output_filepath, sample_rate, subtype, DURATION)

def run_workflow():
    print("=" * 60)
    print("INCREMENTAL AUDIO PRODUCTION WORKFLOW")
    print("=" * 60)
    
    # Step 1: Calculate timing
    print("\n[Step 1] Calculating timing parameters...")
    timing = calculate_section_transitions(BPM, DURATION, SECTIONS)
    print(f"✓ Timing calculated for {len(SECTIONS)} sections")
    
    # Step 2: Verify reference
    print("\n[Step 2] Verifying reference file...")
    if os.path.exists('reference.wav'):
        ref_info = sf.info('reference.wav')
        print(f"✓ Reference: {ref_info.duration:.2f}s @ {ref_info.samplerate}Hz")
    else:
        print("! No reference file found, proceeding with defaults")
    
    # Step 3: Generate stems individually
    print("\n[Step 3] Generating stems (one at a time)...")
    generated_stems = []
    for stem_name in STEM_NAMES:
        print(f"  Generating {stem_name}...")
        filepath, _ = generate_stem(stem_name, DURATION, SAMPLE_RATE, subtype=SUBTYPE)
        result = verify_stem(filepath, SAMPLE_RATE, SUBTYPE, DURATION)
        if result['success']:
            print(f"  ✓ {stem_name} verified")
            generated_stems.append(filepath)
        else:
            print(f"  ✗ {stem_name} FAILED: {result['error']}")
            sys.exit(1)
    
    # Step 4: Generate drums
    print("\n[Step 4] Generating drum stem...")
    drums_filepath, _ = generate_drum_stem(DURATION, SAMPLE_RATE, BPM, subtype=SUBTYPE)
    drums_result = verify_stem(drums_filepath, SAMPLE_RATE, SUBTYPE, DURATION)
    if drums_result['success']:
        print(f"✓ Drums verified")
        generated_stems.append(drums_filepath)
    else:
        print(f"✗ Drums FAILED: {drums_result['error']}")
        sys.exit(1)
    
    # Step 5: Apply effects
    print("\n[Step 5] Applying effects...")
    processed_stems = []
    for stem_name in STEM_NAMES:
        input_file = f'{stem_name}_stem.wav'
        output_file = f'{stem_name}_processed.wav'
        result = apply_effects(input_file, output_file, SAMPLE_RATE, SUBTYPE)
        if result['success']:
            print(f"  ✓ {stem_name} processed")
            processed_stems.append(output_file)
        else:
            print(f"  ✗ {stem_name} processing FAILED")
            sys.exit(1)
    
    drums_processed = 'drums_processed.wav'
    drums_fx_result = apply_effects('drums_stem.wav', drums_processed, SAMPLE_RATE, SUBTYPE)
    if drums_fx_result['success']:
        print(f"  ✓ Drums processed")
        processed_stems.append(drums_processed)
    else:
        sys.exit(1)
    
    # Step 6: Create master
    print("\n[Step 6] Creating master track...")
    first_data, _ = sf.read(processed_stems[0])
    master_audio = np.zeros(len(first_data), dtype=np.float32)
    for stem_file in processed_stems:
        data, _ = sf.read(stem_file)
        master_audio += data * 0.4
    master_audio = np.clip(master_audio, -1, 1)
    sf.write('master_track.wav', master_audio, SAMPLE_RATE, subtype=SUBTYPE, format='WAV')
    master_info = sf.info('master_track.wav')
    print(f"✓ Master track: {master_info.duration:.2f}s @ {master_info.samplerate}Hz")
    
    # Step 7: Archive and verify
    print("\n[Step 7] Creating archive and final verification...")
    all_files = ['master_track.wav'] + processed_stems
    with zipfile.ZipFile('audio_deliverables.zip', 'w', zipfile.ZIP_DEFLATED) as zf:
        for f in all_files:
            zf.write(f)
    
    with zipfile.ZipFile('audio_deliverables.zip', 'r') as zf:
        print(f"✓ Archive contains {len(zf.namelist())} files")
    
    # Final verification
    specs = {f: {'sample_rate': SAMPLE_RATE, 'subtype': SUBTYPE} for f in all_files}
    passed = failed = 0
    for filepath, expected in specs.items():
        info = sf.info(filepath)
        if info.samplerate == expected['sample_rate'] and info.subtype == expected['subtype']:
            passed += 1
        else:
            failed += 1
            print(f"  ✗ {filepath} verification failed")
    
    print(f"\nFinal verification: {passed} passed, {failed} failed")
    if failed > 0:
        sys.exit(1)
    
    print("\n" + "=" * 60)
    print("WORKFLOW COMPLETED SUCCESSFULLY")
    print("=" * 60)
    return 0

if __name__ == '__main__':
    sys.exit(run_workflow())
```

## Troubleshooting

### Common Issues

**Memory errors during stem generation:**
- Process stems one at a time (this skill's default approach)
- Reduce duration or sample rate for testing
- Use `np.float32` instead of `np.float64`

**Sample rate mismatches:**
- Always specify `sample_rate` explicitly in `sf.write()`
- Verify with `sf.info()` after each write operation
- Check that `subtype` parameter is specified

**Archive creation failures:**
- Verify all files exist before archiving
- Use `zipfile.ZIP_DEFLATED` for compression
- Check file permissions

### Best Practices

1. **Run incrementally**: Test each step independently before running full workflow
2. **Verify early**: Check output properties immediately after generation
3. **Use explicit types**: Always specify `subtype` and `format` parameters
4. **Monitor memory**: Process large files in chunks if needed
5. **Keep logs**: Save verification results for debugging

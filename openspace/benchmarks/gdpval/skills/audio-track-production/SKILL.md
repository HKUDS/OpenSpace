---
name: audio-track-production
description: End-to-end audio production workflow with stems, effects, archiving, and verification
---

# Audio Track Production Workflow

This skill provides a reusable pattern for executing audio production tasks that require generating a master track and multiple stems, applying effects, and delivering verified outputs in an archive.

## Overview

Follow these steps in order to ensure consistent, verifiable audio production outputs:

1. Verify reference audio file
2. Calculate timing parameters from BPM and duration
3. Generate stems with explicit sample type specifications
4. Apply audio effects via signal processing
5. Export master track and all stems
6. Archive deliverables in zip format
7. Verify all outputs match specifications

## Step 1: Verify Reference File

Before processing, verify the reference audio file is valid and readable:

```python
import soundfile as sf

# Verify reference file exists and is readable
info = sf.info('reference_track.wav')
print(f"Sample rate: {info.samplerate} Hz")
print(f"Duration: {info.frames / info.samplerate:.2f} seconds")
print(f"Channels: {info.channels}")
print(f"Subtype: {info.subtype}")
```

## Step 2: Calculate Timing Parameters

Derive timing for key section transitions from BPM and total duration:

```python
def calculate_section_transitions(bpm, total_duration_sec, sections):
    """Calculate beat-aligned transition points for song sections."""
    beats_per_second = bpm / 60.0
    total_beats = total_duration_sec * beats_per_second
    
    # Distribute sections proportionally or by specified ratios
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

# Example usage
sections = calculate_section_transitions(
    bpm=120,
    total_duration_sec=137,
    sections={'intro': 16, 'verse': 32, 'chorus': 32, 'bridge': 16, 'outro': 16}
)
```

## Step 3: Generate Stems with Explicit Sample Type

Always specify sample type explicitly when generating stems to ensure bit-depth consistency:

```python
import numpy as np
import soundfile as sf

def generate_stem(name, duration_sec, sample_rate, subtype='FLOAT'):
    """Generate a stem with explicit sample type specification."""
    frames = int(duration_sec * sample_rate)
    
    # Generate audio content (replace with actual synthesis/processing)
    t = np.linspace(0, duration_sec, frames)
    audio_data = np.sin(2 * np.pi * 440 * t)  # Example: 440Hz tone
    
    # Ensure proper data type for specified subtype
    if subtype == 'FLOAT':
        audio_data = audio_data.astype(np.float32)
    elif subtype == 'PCM_24':
        audio_data = np.clip(audio_data, -1, 1) * (2**23 - 1)
        audio_data = audio_data.astype(np.int32)
    
    sf.write(
        f'{name}_stem.wav',
        audio_data,
        sample_rate,
        subtype=subtype,  # Explicit subtype for 24-bit float or other
        format='WAV'
    )
    return audio_data

# Example: Generate 4 stems at 48kHz, 137s, 24-bit float
sample_rate = 48000
duration = 137
stems = ['guitars', 'synths', 'bridge', 'bass']

for stem_name in stems:
    generate_stem(stem_name, duration, sample_rate, subtype='FLOAT')
```

## Step 4: Apply Effects via scipy.signal

Use scipy.signal for applying audio effects and processing:

```python
from scipy import signal
import numpy as np

def apply_lowpass_filter(audio_data, sample_rate, cutoff_freq=5000):
    """Apply a lowpass filter using scipy.signal."""
    nyquist = sample_rate / 2
    normalized_cutoff = cutoff_freq / nyquist
    
    # Design Butterworth filter
    b, a = signal.butter(4, normalized_cutoff, btype='low')
    
    # Apply filter
    filtered_data = signal.filtfilt(b, a, audio_data)
    return filtered_data

def apply_reverb_simple(audio_data, sample_rate, decay=0.5, delay_samples=1000):
    """Apply simple reverb effect."""
    reverbed = np.copy(audio_data)
    decay_factor = decay
    
    for i in range(1, 6):
        delayed = np.zeros_like(audio_data)
        if len(audio_data) > delay_samples * i:
            delayed[delay_samples * i:] = audio_data[:-delay_samples * i]
        reverbed += delayed * (decay_factor ** i)
    
    return np.clip(reverbed, -1, 1)

# Apply effects to stems
for stem_name in stems:
    data, sr = sf.read(f'{stem_name}_stem.wav')
    processed = apply_lowpass_filter(data, sr, cutoff_freq=8000)
    processed = apply_reverb_simple(processed, sr, decay=0.3)
    sf.write(f'{stem_name}_stem_processed.wav', processed, sr, subtype='FLOAT')
```

## Step 5: Export Master and Stems

Export all final deliverables with consistent specifications:

```python
def export_audio(filepath, audio_data, sample_rate, subtype='FLOAT'):
    """Export audio file with verified specifications."""
    sf.write(
        filepath,
        audio_data,
        sample_rate,
        subtype=subtype,
        format='WAV'
    )
    # Verify export
    info = sf.info(filepath)
    assert info.samplerate == sample_rate, f"Sample rate mismatch: {info.samplerate}"
    assert info.subtype == subtype, f"Subtype mismatch: {info.subtype}"
    print(f"Exported: {filepath} ({info.duration:.2f}s, {info.samplerate}Hz)")

# Export master (mix of all stems)
master_audio = np.zeros_like(stem_audio)  # Replace with actual mix
for stem_name in stems:
    stem_data, _ = sf.read(f'{stem_name}_stem_processed.wav')
    master_audio += stem_data * 0.5  # Simple mix with gain staging

master_audio = np.clip(master_audio, -1, 1)
export_audio('master_track.wav', master_audio, sample_rate=48000, subtype='FLOAT')

# Export final stems
for stem_name in stems:
    stem_data, sr = sf.read(f'{stem_name}_stem_processed.wav')
    export_audio(f'{stem_name}.wav', stem_data, sample_rate=48000, subtype='FLOAT')
```

## Step 6: Archive Deliverables

Package all outputs in a zip archive:

```python
import zipfile
import os

def create_archive(archive_name, file_list):
    """Create zip archive of deliverables."""
    with zipfile.ZipFile(archive_name, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for filepath in file_list:
            if os.path.exists(filepath):
                zipf.write(filepath, os.path.basename(filepath))
                print(f"Added to archive: {filepath}")
            else:
                print(f"WARNING: File not found: {filepath}")
    
    # Verify archive
    with zipfile.ZipFile(archive_name, 'r') as zipf:
        contents = zipf.namelist()
        print(f"Archive contains {len(contents)} files: {contents}")
    
    return archive_name

# Archive master and stems
deliverables = ['master_track.wav'] + [f'{stem}.wav' for stem in stems]
create_archive('audio_deliverables.zip', deliverables)
```

## Step 7: Verify All Outputs

Final verification that all outputs match specifications:

```python
def verify_outputs(expected_specs):
    """Verify all output files match expected specifications."""
    results = {'passed': 0, 'failed': 0, 'details': []}
    
    for filepath, specs in expected_specs.items():
        if not os.path.exists(filepath):
            results['failed'] += 1
            results['details'].append(f"MISSING: {filepath}")
            continue
        
        info = sf.info(filepath)
        errors = []
        
        if specs.get('sample_rate') and info.samplerate != specs['sample_rate']:
            errors.append(f"sample_rate: expected {specs['sample_rate']}, got {info.samplerate}")
        
        if specs.get('subtype') and info.subtype != specs['subtype']:
            errors.append(f"subtype: expected {specs['subtype']}, got {info.subtype}")
        
        if specs.get('min_duration') and info.duration < specs['min_duration']:
            errors.append(f"duration: expected >= {specs['min_duration']}s, got {info.duration}s")
        
        if errors:
            results['failed'] += 1
            results['details'].append(f"FAILED: {filepath} - {'; '.join(errors)}")
        else:
            results['passed'] += 1
            results['details'].append(f"PASSED: {filepath} ({info.duration:.2f}s, {info.samplerate}Hz, {info.subtype})")
    
    return results

# Verification specifications
expected_specs = {
    'master_track.wav': {'sample_rate': 48000, 'subtype': 'FLOAT', 'min_duration': 137},
    'guitars.wav': {'sample_rate': 48000, 'subtype': 'FLOAT', 'min_duration': 137},
    'synths.wav': {'sample_rate': 48000, 'subtype': 'FLOAT', 'min_duration': 137},
    'bridge.wav': {'sample_rate': 48000, 'subtype': 'FLOAT', 'min_duration': 137},
    'bass.wav': {'sample_rate': 48000, 'subtype': 'FLOAT', 'min_duration': 137},
}

verification = verify_outputs(expected_specs)
print(f"\nVerification: {verification['passed']} passed, {verification['failed']} failed")
for detail in verification['details']:
    print(detail)

assert verification['failed'] == 0, "Output verification failed!"
```

## Complete Workflow Example

```python
#!/usr/bin/env python3
"""Complete audio production workflow execution."""

import soundfile as sf
import numpy as np
from scipy import signal
import zipfile
import os

# Configuration
SAMPLE_RATE = 48000
DURATION = 137
BPM = 120
STEM_NAMES = ['guitars', 'synths', 'bridge', 'bass']
SUBTYPE = 'FLOAT'

def run_workflow():
    # Step 1: Verify reference
    ref_info = sf.info('reference.wav')
    print(f"Reference: {ref_info.duration}s @ {ref_info.samplerate}Hz")
    
    # Step 2: Calculate timing
    bpm = BPM
    beats_per_sec = bpm / 60
    
    # Step 3-4: Generate and process stems
    for stem in STEM_NAMES:
        frames = int(DURATION * SAMPLE_RATE)
        t = np.linspace(0, DURATION, frames)
        audio = np.sin(2 * np.pi * 220 * t)  # Example content
        
        # Apply effects
        audio = apply_lowpass_filter(audio, SAMPLE_RATE, 8000)
        
        # Export with explicit subtype
        sf.write(f'{stem}.wav', audio, SAMPLE_RATE, subtype=SUBTYPE)
    
    # Step 5: Export master
    master = np.zeros(int(DURATION * SAMPLE_RATE))
    for stem in STEM_NAMES:
        data, _ = sf.read(f'{stem}.wav')
        master += data * 0.5
    master = np.clip(master, -1, 1)
    sf.write('master_track.wav', master, SAMPLE_RATE, subtype=SUBTYPE)
    
    # Step 6: Archive
    files = ['master_track.wav'] + [f'{s}.wav' for s in STEM_NAMES]
    with zipfile.ZipFile('deliverables.zip', 'w') as zf:
        for f in files:
            zf.write(f)
    
    # Step 7: Verify
    specs = {f: {'sample_rate': SAMPLE_RATE, 'subtype': SUBTYPE} for f in files}
    results = verify_outputs(specs)
    assert results['failed'] == 0
    print("Workflow complete!")

if __name__ == '__main__':
    run_workflow()
```

## Key Principles

- **Explicit sample types**: Always specify `subtype` parameter (e.g., `subtype='FLOAT'` for 24-bit float WAV)
- **Verify at each step**: Check file properties after each major operation
- **Consistent specifications**: Maintain same sample rate, bit depth, and duration across all outputs
- **Archive for delivery**: Package all deliverables together for easy distribution
- **Final verification**: Assert all outputs meet specifications before declaring success
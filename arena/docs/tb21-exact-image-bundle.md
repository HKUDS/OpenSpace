# Transfer Terminal-Bench 2.1 Images

After [verifier preparation](tb21-offline-verifier.md), run these optional transfer commands from `arena/`.

Use the verified `offline-verifier-authority.json` from preparation. The bundle preserves its exact base and verifier image IDs; it does not rebuild or pull replacements. Replace the paths and Docker sockets below with your source and destination locations.

## ① Audit and export

```bash
uv run --no-sync rsi-arena-tb21-image-bundle \
  --docker-host unix:///path/to/docker.sock \
  --authority /path/to/offline-verifier-authority.json \
  audit
```

```bash
uv run --no-sync rsi-arena-tb21-image-bundle \
  --docker-host unix:///path/to/source-docker.sock \
  --authority /path/to/offline-verifier-authority.json \
  export \
  --output /path/to/tb21-images.tar \
  --manifest /path/to/tb21-images.json
```

The source authority must be fully verified and every image must be present. Copy the authority file, bundle and manifest to the destination.

## ② Import

```bash
uv run --no-sync rsi-arena-tb21-image-bundle \
  --docker-host unix:///path/to/destination-docker.sock \
  --authority /path/to/offline-verifier-authority.json \
  import \
  --bundle /path/to/tb21-images.tar \
  --manifest /path/to/tb21-images.json
```

Import checks the bundle digest, authority and reconstructed image IDs. Configure the experiment to use the destination authority path and Docker socket.

# API Tools

REST API for PDF, Video, Audio and Image file manipulation with API Key authentication.

## Features

### PDF Manipulation
- **Split PDF**: Extract specific pages from a PDF
- **Extract Pages**: Separate all pages into individual files
- **Merge PDFs**: Combine multiple PDFs into a single file
- **Add Password**: Add password protection to PDF
- **Remove Password**: Remove password from a protected PDF
- **PDF Info**: Get PDF information and metadata

### PDF Conversion
- **Convert to Image**: Convert PDF to PNG, JPEG, or TIFF
- **Convert to OFX**: Extract transactions from bank statements to OFX
- **Extract Text**: Extract text from all PDF pages

### Video Manipulation
- **Cut Video**: Extract a segment from a video by start/end time
- **Transcribe Video**: Transcribe video audio to text with timestamps

### Audio Manipulation
- **Cut Audio**: Extract a segment from an audio file by start/end time
- **Transcribe Audio**: Transcribe audio to text with timestamps

### Image Manipulation
- **Convert Image**: Convert between formats (JPG, PNG, WebP, GIF, BMP, TIFF, SVG); HEIC/HEIF accepted as input on every image endpoint
- **Compress Image**: Reduce image file size with quality control
- **Images to PDF**: Combine multiple images into a single PDF

## Architecture

```
app/
├── main.py              # FastAPI application
├── auth_secure.py       # Authentication with rate limiting
├── config.py            # Configuration
├── routers/
│   ├── pdfRoute.py      # PDF endpoints
│   ├── videoRoute.py    # Video endpoints
│   ├── audioRoute.py    # Audio endpoints
│   └── imageRoute.py    # Image endpoints
├── services/
│   ├── pdfService.py    # PDF processing logic
│   ├── videoService.py  # Video processing logic
│   ├── audioService.py  # Audio processing logic
│   └── imageService.py  # Image processing logic
└── utils/
    ├── filename.py      # Filename utilities
    ├── pagination.py    # Page range parser
    └── security.py      # Security validations
```

## Supported Formats

### Video
- MP4, AVI, MOV, MKV, WebM, WMV, FLV, M4V

### Audio
- MP3, WAV, M4A, OGG, FLAC, AAC, WMA

### Image
- Input: JPG, JPEG, PNG, GIF, BMP, WebP, TIFF, TIF, SVG, HEIC, HEIF
- Output: JPEG, PNG, WebP, GIF, BMP, TIFF
- Note: SVG → Raster conversion supported (via CairoSVG)

### Transcription Languages
- Portuguese (pt), English (en), Spanish (es), French (fr), German (de)
- Italian (it), Japanese (ja), Chinese (zh), Korean (ko), Russian (ru)
- Arabic (ar), Hindi (hi), Dutch (nl), Polish (pl), Turkish (tr)

## Security

### File Protection
- Magic bytes validation (verifies actual file content)
- Size limit per file (50MB for PDF, 100MB for ZIP)
- Request size limit enforced by the API, returned as JSON 413: 50MB per request by default,
  5GB for `/audio` and `/movie` (`MAX_REQUEST_SIZE`, `MAX_MEDIA_REQUEST_SIZE`). The limit
  covers the whole request, so multi-file endpoints are capped on the sum of the files
- The reverse proxy must not cap the body (`client_max_body_size 0;` on nginx): nginx's 1MB
  default answers 413 in HTML before the request reaches the API
- Filename sanitization
- Path traversal protection
- Limit of 20 files per merge

### Authentication
- API Key with timing-safe comparison
- Rate limiting (100 req/min per IP)
- Blocking after 10 failed attempts (5 min)
- Authentication attempt logging

## Requirements

- Docker
- Docker Compose
- FFmpeg (included in Docker image)

## Configuration

1. Copy the environment variables example file:
```bash
cp .env.example .env
```

2. Edit the `.env` file and set your API Key:
```
API_KEY=your-secret-key-here
```

## Running

### With Docker Compose

```bash
docker-compose up -d --build
```

### Local Development

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

## Tests

```bash
docker exec trem-api pytest -v

docker exec trem-api pytest tests/test_services.py -v
docker exec trem-api pytest tests/test_utils.py -v
docker exec trem-api pytest tests/test_routes.py -v
docker exec trem-api pytest tests/test_security.py -v
```

Migrations run against a real PostgreSQL whose name contains `test` (its schema is dropped;
skipped without the variable):

```bash
docker run -d --name trem-test-pg -e POSTGRES_PASSWORD=test -e POSTGRES_DB=trem_test \
  -p 127.0.0.1:55432:5432 postgres:16-alpine
TEST_DATABASE_URL=postgresql+asyncpg://postgres:test@127.0.0.1:55432/trem_test pytest tests/test_migrations.py -v
```

After each deploy, check uploads through Cloudflare and the reverse proxy (skipped without the variables):

```bash
SMOKE_API_URL=https://api.didilv93.com SMOKE_API_KEY=... pytest tests/test_deployed_uploads.py -v
```

## Endpoints

All endpoints require the `X-API-Key` header.

### Health Check
```
GET /health
```

### PDF Endpoints

#### Split PDF
```
POST /pdf/split
Content-Type: multipart/form-data

file: document.pdf
pages: "1-3,5,7-10"
```

#### Extract Pages
```
POST /pdf/extract-pages
Content-Type: multipart/form-data

file: document.pdf
```

#### Merge PDFs
```
POST /pdf/merge
Content-Type: multipart/form-data

files: document1.pdf
files: document2.pdf
```

#### Add Password
```
POST /pdf/add-password
Content-Type: multipart/form-data

file: document.pdf
user_password: password123
owner_password: admin_password (optional)
```

#### Remove Password
```
POST /pdf/remove-password
Content-Type: multipart/form-data

file: document.pdf
password: password123
```

#### PDF Info
```
POST /pdf/info
Content-Type: multipart/form-data

file: document.pdf
```

#### Convert to Image
```
POST /pdf/convert-to-image
Content-Type: multipart/form-data

file: document.pdf
format: png | jpeg | tiff (default: png)
dpi: 72-600 (default: 150)
pages: "1-3,5" (optional)
```

#### Convert to OFX
```
POST /pdf/convert-to-ofx
Content-Type: multipart/form-data

file: statement.pdf
bank_id: 001
account_id: 12345678
account_type: CHECKING | SAVINGS | CREDITCARD
```

#### Extract Text
```
POST /pdf/extract-text
Content-Type: multipart/form-data

file: document.pdf
```

### Video Endpoints

#### Cut Video
```
POST /movie/cut
Content-Type: multipart/form-data

file: video.mp4
start: 30 (seconds)
end: 60 (seconds)
```

#### Transcribe Video
```
POST /movie/transcribe
Content-Type: multipart/form-data

file: video.mp4
language: pt (optional, auto-detect if empty)
```

Transcription runs as a background job. The response is `202` with the job id:
```json
{"job_id": "5f0c...", "status": "queued"}
```

Poll `GET /movie/transcribe/jobs/{job_id}` until `status` is `done` or `failed`:
```json
{
  "job_id": "5f0c...",
  "status": "done",
  "result": {
    "text": "Full transcription...",
    "segments": [
      {"start": 0.0, "end": 2.5, "text": "Hello world"},
      {"start": 2.5, "end": 5.0, "text": "How are you?"}
    ],
    "language": "en",
    "duration": 120.5
  }
}
```
A failed job carries `error` instead of `result`.

#### Transcribe large files (upload in parts)

Cloudflare rejects request bodies over 100 MB and responses slower than 100 s, so
large files are sent in parts. Same flow under `/audio`:

1. `POST /movie/transcribe/uploads` with JSON `{"filename": "video.mp4", "size": <bytes>, "language": "pt"}`
   returns `{"upload_id", "part_size", "total_parts"}`
2. `PUT /movie/transcribe/uploads/{upload_id}/parts/{index}` with the raw bytes of each part
   (`application/octet-stream`), index `0` to `total_parts - 1`. Parts may be retried and
   sent in any order; each must be exactly `part_size` bytes, except the last
3. `POST /movie/transcribe/uploads/{upload_id}/complete` returns the job, polled as above

`DELETE /movie/transcribe/uploads/{upload_id}` cancels an upload and deletes its parts.

Jobs and uploads are kept in memory, scoped to the caller, and run one at a time.
Their files live under `UPLOAD_DIR/transcriptions`, one directory each, removed when the
job ends or the upload is cancelled; the directory is wiped at startup. Idle uploads
and results expire after `TRANSCRIPTION_TTL_SECONDS` (24 h). Part size:
`TRANSCRIPTION_PART_SIZE` (50 MB). The API must run as a single process.

### Audio Endpoints

#### Cut Audio
```
POST /audio/cut
Content-Type: multipart/form-data

file: audio.mp3
start: 30 (seconds)
end: 60 (seconds)
```

#### Transcribe Audio
```
POST /audio/transcribe
Content-Type: multipart/form-data

file: audio.mp3
language: pt (optional, auto-detect if empty)
```

Returns a job, like `/movie/transcribe`; poll `GET /audio/transcribe/jobs/{job_id}`.
Large files use the upload in parts under `/audio/transcribe/uploads`.

### Image Endpoints

#### Convert Image
```
POST /image/convert
Content-Type: multipart/form-data

file: image.png
format: jpeg | png | webp | gif | bmp | tiff
quality: 1-100 (default: 95)
```

#### Compress Image
```
POST /image/compress
Content-Type: multipart/form-data

file: image.jpg
quality: 1-100 (default: 70, lower = smaller)
max_dimension: 1920 (optional, max width/height)
response_type: file | json (default: file)
```

Response (with `response_type=json`):
```json
{
  "metrics": {
    "original_size_bytes": 1500000,
    "compressed_size_bytes": 350000,
    "reduction_percent": 76.67,
    "original_dimensions": {"width": 1920, "height": 1080},
    "final_dimensions": {"width": 1920, "height": 1080}
  },
  "file": {
    "filename": "image_compressed.jpg",
    "media_type": "image/jpeg",
    "size_bytes": 350000,
    "base64": "/9j/4AAQSkZJRgABAQAA..."
  }
}
```

#### Images to PDF
```
POST /image/to-pdf
Content-Type: multipart/form-data

files: image1.jpg
files: image2.png
layout: single | grouped (default: single)
images_per_page: 1-9 (default: 4, only for grouped)
```

### LLM Endpoints

OpenAI-compatible chat completion backed by the `trem-llm` container. Slow (a cold
model takes minutes to load, a long prompt can take minutes to answer), so over the
public URL use the [job endpoints](#chat-job): Cloudflare cuts responses at 100 s and
nginx at 60 s. The synchronous endpoint is for callers on the local port. One inference
runs at a time, jobs included; when the queue is full the API answers `503` with
`Retry-After`. Every `/llm` route requires the `ai` profile (`403` without it).

#### Chat Completion
```
POST /llm/v1/chat/completions
Content-Type: application/json

{"messages": [{"role": "user", "content": "Reply with OK."}], "max_tokens": 64}
```

`stream` is forced to `false`, `model` is ignored and `max_tokens` is capped by the
server. The llama-server response is returned as is (`choices`, `usage`, `timings`).

The model sleeps after 15 min idle. A call to a sleeping model gets an immediate `503`
and the API starts waking it; retry after `Retry-After` seconds:

```json
{"status": "warming_up", "detail": "Model is waking up, retry after the indicated time",
 "elapsed_seconds": 40, "eta_seconds": 170, "progress": 0.19}
```

`eta_seconds` and `progress` are estimates from the measured reload time.

With the OpenAI SDK, send the trem-api key as `X-API-Key` (the SDK's own Bearer is not used):

```python
OpenAI(base_url="http://localhost:13002/llm/v1", api_key="unused",
       default_headers={"X-API-Key": "trem_..."})
```

#### Chat Job
```
POST /llm/jobs
Content-Type: application/json

{"messages": [{"role": "user", "content": "Reply with OK."}], "max_tokens": 64}
```

Same body and rules as the chat completion, answered at once with `202`:

```json
{"job_id": "3f2a...", "status": "queued"}
```

Poll the job until it ends:

```
GET /llm/jobs/{job_id}
```

`status` is `queued`, `running`, `done` (with `result`, the llama-server response as is)
or `failed` (with `error`). A sleeping model is woken inside the job, so there is no
`warming_up` answer here; the job just takes longer. Finished jobs expire after 1 h
(`LLM_JOB_TTL_SECONDS`) and are lost if the API restarts.

#### LLM Health
```
GET /llm/health
```

Returns `{"status": "healthy" | "sleeping" | "warming_up"}` without waking the model,
or `503` when the model server is unreachable.

## Usage Examples

### cURL

```bash
curl -X POST "http://localhost:3002/pdf/split" \
  -H "X-API-Key: your-key" \
  -F "file=@document.pdf" \
  -F "pages=1-3" \
  --output result.pdf

curl -X POST "http://localhost:3002/movie/cut" \
  -H "X-API-Key: your-key" \
  -F "file=@video.mp4" \
  -F "start=30" \
  -F "end=60" \
  --output clip.mp4

curl -X POST "http://localhost:3002/audio/transcribe" \
  -H "X-API-Key: your-key" \
  -F "file=@audio.mp3" \
  -F "language=pt"

# Image compression with metrics
curl -X POST "http://localhost:3002/image/compress" \
  -H "X-API-Key: your-key" \
  -F "file=@photo.jpg" \
  -F "quality=60" \
  -F "response_type=json"

# Convert image format
curl -X POST "http://localhost:3002/image/convert" \
  -H "X-API-Key: your-key" \
  -F "file=@image.png" \
  -F "format=webp" \
  --output image.webp

# Images to PDF
curl -X POST "http://localhost:3002/image/to-pdf" \
  -H "X-API-Key: your-key" \
  -F "files=@img1.jpg" \
  -F "files=@img2.png" \
  -F "layout=grouped" \
  -F "images_per_page=4" \
  --output album.pdf
```

### Python

```python
import os
import time

import requests

base = "http://localhost:3002/movie/transcribe"
headers = {"X-API-Key": "your-key"}

# Upload in parts (works for any size)
path = "video.mp4"
upload = requests.post(
    f"{base}/uploads",
    headers=headers,
    json={"filename": os.path.basename(path), "size": os.path.getsize(path), "language": "pt"},
).json()
with open(path, "rb") as f:
    for index in range(upload["total_parts"]):
        requests.put(
            f"{base}/uploads/{upload['upload_id']}/parts/{index}",
            headers=headers,
            data=f.read(upload["part_size"]),
        ).raise_for_status()
job = requests.post(f"{base}/uploads/{upload['upload_id']}/complete", headers=headers).json()

while job["status"] not in ("done", "failed"):
    time.sleep(3)
    job = requests.get(f"{base}/jobs/{job['job_id']}", headers=headers).json()
print(job.get("result") or job["error"])

# Image compression
url = "http://localhost:3002/image/compress"
files = {"file": open("photo.jpg", "rb")}
data = {"quality": 60, "response_type": "json"}

response = requests.post(url, headers=headers, files=files, data=data)
metrics = response.json()["metrics"]
print(f"Reduced {metrics['reduction_percent']}%")
```

## Interactive Documentation

Swagger UI: `http://localhost:3002/docs`

## License

MIT

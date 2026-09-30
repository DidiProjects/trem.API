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
  100MB for `/audio` and `/movie` (`MAX_REQUEST_SIZE`, `MAX_MEDIA_REQUEST_SIZE`). The limit
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

Response:
```json
{
  "text": "Full transcription...",
  "segments": [
    {"start": 0.0, "end": 2.5, "text": "Hello world"},
    {"start": 2.5, "end": 5.0, "text": "How are you?"}
  ],
  "language": "en",
  "duration": 120.5
}
```

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
import requests

url = "http://localhost:3002/movie/transcribe"
headers = {"X-API-Key": "your-key"}
files = {"file": open("video.mp4", "rb")}
data = {"language": "pt"}

response = requests.post(url, headers=headers, files=files, data=data)
print(response.json())

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

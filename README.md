# Raspberry Pi Zero 2 W Camera Web Application

This project is a high-performance web application built with **FastAPI** to stream video and capture images from a Raspberry Pi Zero 2 W using the official `picamera2` library.

## Features
- **Real-time MJPEG Streaming**: Low-latency video feed accessible via any web browser.
- **Dynamic Resolution Switching**: Change camera resolution (SD, HD, FHD) on-the-fly without restarting the server.
- **Image Capture**: Capture high-quality JPEG images and download them directly from the interface.
- **Asynchronous Backend**: Leverages FastAPI and Uvicorn for efficient handling of concurrent stream requests.

## Prerequisites
- **Hardware**: Raspberry Pi Zero 2 W (or newer) and a compatible Raspberry Pi Camera Module.
- **OS**: Raspberry Pi OS (Bullseye or Bookworm) with the `libcamera` stack enabled.
- **Python**: Version 3.12 (as specified in `.python-version`).

## Installation

1. **Install System Dependencies**:
   Ensure the Pi camera library is installed on your system:
   ```bash
   sudo apt update
   sudo apt install python3-picamera2
   ```

2. **Install Python Packages**:
   ```bash
   pip install fastapi uvicorn pydantic jinja2
   ```

## Running the Application

Start the server by running `app.py`:
```bash
python app.py
```
The application will be available at `http://<your-pi-ip>:5000`.

## Project Structure
- `app.py`: The main FastAPI application logic and camera control.
- `templates/index.html`: The frontend user interface.
- `main.py`: Entry point for basic testing.

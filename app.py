import io
import time
from fastapi import FastAPI, Request, HTTPException
+from fastapi.responses import StreamingResponse, FileResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from picamera2 import Picamera2

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# Initialize Camera
picam2 = Picamera2()
config = picam2.create_video_configuration(main={"size": (640, 480)})
picam2.configure(config)
picam2.start()

class Resolution(BaseModel):
    width: int
    height: int

def generate_frames():
    while True:
        buf = io.BytesIO()
        picam2.capture_file(buf, format="jpeg")
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + buf.getvalue() + b'\r\n')

@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/video_feed")
async def video_feed():
    return StreamingResponse(
        generate_frames(),
        media_type="multipart/x-mixed-replace; boundary=frame"
    )

@app.post("/change_resolution")
async def change_resolution(res: Resolution):
    try:
        picam2.stop()
        new_config = picam2.create_video_configuration(main={"size": (res.width, res.height)})
        picam2.configure(new_config)
        picam2.start()
        return {"status": "success", "resolution": f"{res.width}x{res.height}"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/capture")
async def capture():
    timestamp = int(time.time())
    filename = f"capture_{timestamp}.jpg"
    picam2.capture_file(filename)
    return FileResponse(path=filename, filename=filename, media_type='image/jpeg')

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='0.0.0.0', port=5000)

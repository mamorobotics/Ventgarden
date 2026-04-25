#!/usr/bin/env python3
"""
MJPEG Stream Emulator for ustreamer testing

This script emulates the output from ustreamer (µStreamer), serving an MJPEG stream
on a specific URL. It can stream from either a video file or a series of static images.

Usage:
    # From video file with 30 FPS (looping by default)
    python mjpeg_emulator.py --video path/to/video.mp4 --fps 30

    # From video file with no looping (plays once)
    python mjpeg_emulator.py --video path/to/video.mp4 --fps 30 --no-loop

    # From image sequence
    python mjpeg_emulator.py --images path/to/images/*.jpg --fps 10

    # Custom host/port
    python mjpeg_emulator.py --video video.mp4 --host 0.0.0.0 --port 8080
"""

import argparse
import io
import os
import time
import threading
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import logging

import cv2
import numpy as np

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# MJPEG stream boundary
BOUNDARY = "ustreamer"


class FrameBuffer:
    """Thread-safe frame buffer for storing the current frame."""
    
    def __init__(self):
        self.frame = None
        self.frame_lock = threading.Lock()
        self.frame_count = 0
        self.width = 0
        self.height = 0
    
    def update_frame(self, frame_data, width, height):
        """Update the current frame."""
        with self.frame_lock:
            self.frame = frame_data
            self.width = width
            self.height = height
            self.frame_count += 1
    
    def get_frame(self):
        """Get the current frame."""
        with self.frame_lock:
            return self.frame, self.width, self.height, self.frame_count


class MJPEGHandler(BaseHTTPRequestHandler):
    """HTTP request handler for MJPEG streaming."""
    
    # Class-level frame buffer shared across all instances
    frame_buffer = None
    
    def do_GET(self):
        """Handle GET requests."""
        if self.path == '/':
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            
            html = """
            <html>
            <head><title>µStreamer Emulator</title></head>
            <body>
                <h1>µStreamer Emulator</h1>
                <p>MJPEG stream available at: <a href="/stream">/stream</a></p>
                <img src="/stream" style="max-width: 100%;" alt="MJPEG Stream">
            </body>
            </html>
            """
            self.wfile.write(html.encode())
        
        elif self.path == '/stream':
            self.send_response(200)
            self.send_header('Content-Type', f'multipart/x-mixed-replace; boundary={BOUNDARY}')
            self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
            self.send_header('Pragma', 'no-cache')
            self.send_header('Expires', '0')
            self.send_header('Connection', 'close')
            self.end_headers()
            
            # Stream frames
            self._stream_frames()
        
        else:
            self.send_response(404)
            self.end_headers()
    
    def _stream_frames(self):
        """Stream MJPEG frames to the client."""
        try:
            while True:
                frame_data, width, height, frame_num = self.frame_buffer.get_frame()
                
                if frame_data is None:
                    time.sleep(0.01)
                    continue
                
                # Write MJPEG boundary and headers
                boundary_line = f"--{BOUNDARY}\r\n"
                self.wfile.write(boundary_line.encode())
                
                # Write frame headers (mimicking ustreamer's behavior)
                headers = (
                    "Content-Type: image/jpeg\r\n"
                    f"Content-Length: {len(frame_data)}\r\n"
                    f"X-UStreamer-Width: {width}\r\n"
                    f"X-UStreamer-Height: {height}\r\n"
                    f"X-UStreamer-Frame: {frame_num}\r\n"
                    "\r\n"
                )
                self.wfile.write(headers.encode())
                
                # Write JPEG data
                self.wfile.write(frame_data)
                self.wfile.write(b"\r\n")
                
        except Exception as e:
            logger.error(f"Streaming error: {e}")
    
    def log_message(self, format, *args):
        """Override to use logger instead of stderr."""
        logger.info(format % args)


class FrameProducer:
    """Base class for frame producers."""
    
    def __init__(self, fps=30):
        self.fps = fps
        self.frame_interval = 1.0 / fps
    
    def get_next_frame(self):
        """Get the next frame. Should return (frame_data, width, height) or (None, 0, 0)."""
        raise NotImplementedError


class VideoFileProducer(FrameProducer):
    """Producer that reads frames from a video file."""
    
    def __init__(self, video_path, fps=None, loop=True):
        self.video_path = video_path
        self.loop = loop
        self.cap = cv2.VideoCapture(video_path)
        
        if not self.cap.isOpened():
            raise ValueError(f"Failed to open video file: {video_path}")
        
        # Use video's FPS if not specified
        if fps is None:
            fps = self.cap.get(cv2.CAP_PROP_FPS)
            if fps <= 0:
                fps = 30
        
        super().__init__(fps)
        
        self.width = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.height = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self.total_frames = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT))
        self.is_finished = False
        
        loop_text = "looping" if self.loop else "non-looping"
        logger.info(f"Video: {self.width}x{self.height} @ {self.fps} FPS ({self.total_frames} frames, {loop_text})")
    
    def get_next_frame(self):
        """Read next frame from video."""
        ret, frame = self.cap.read()
        
        if not ret:
            if self.loop:
                # Loop back to the beginning
                logger.info("Video reached end, looping to start")
                self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                ret, frame = self.cap.read()
                if not ret:
                    return None, 0, 0
            else:
                # Mark as finished and return blank frame
                if not self.is_finished:
                    logger.info("Video reached end (no looping)")
                    self.is_finished = True
                # Continue to return the last frame or a blank frame
                blank = np.zeros((self.height, self.width, 3), dtype=np.uint8)
                cv2.putText(
                    blank,
                    "End of Video",
                    (50, self.height // 2),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    1.5,
                    (255, 255, 255),
                    2
                )
                ret, jpeg = cv2.imencode('.jpg', blank, [cv2.IMWRITE_JPEG_QUALITY, 90])
                if not ret:
                    return None, 0, 0
                return jpeg.tobytes(), self.width, self.height
        
        # Encode to JPEG
        ret, jpeg = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
        if not ret:
            return None, 0, 0
        
        frame_data = jpeg.tobytes()
        return frame_data, self.width, self.height
    
    def cleanup(self):
        """Clean up resources."""
        self.cap.release()


class ImageSequenceProducer(FrameProducer):
    """Producer that cycles through static images."""
    
    def __init__(self, image_paths, fps=10, loop=True):
        super().__init__(fps)
        
        self.image_paths = sorted(image_paths)
        if not self.image_paths:
            raise ValueError("No images found")
        
        self.current_index = 0
        self.loop = loop
        self.is_finished = False
        
        # Load first image to get dimensions
        first_frame = cv2.imread(self.image_paths[0])
        if first_frame is None:
            raise ValueError(f"Failed to load image: {self.image_paths[0]}")
        
        self.height, self.width = first_frame.shape[:2]
        loop_text = "looping" if self.loop else "non-looping"
        logger.info(f"Images: {self.width}x{self.height} @ {self.fps} FPS ({len(self.image_paths)} images, {loop_text})")
    
    def get_next_frame(self):
        """Get next image in sequence."""
        if self.is_finished:
            # Return blank frame if sequence is finished
            blank = np.zeros((self.height, self.width, 3), dtype=np.uint8)
            cv2.putText(
                blank,
                "End of Sequence",
                (50, self.height // 2),
                cv2.FONT_HERSHEY_SIMPLEX,
                1.5,
                (255, 255, 255),
                2
            )
            ret, jpeg = cv2.imencode('.jpg', blank, [cv2.IMWRITE_JPEG_QUALITY, 90])
            if not ret:
                return None, 0, 0
            return jpeg.tobytes(), self.width, self.height
        
        image_path = self.image_paths[self.current_index]
        frame = cv2.imread(image_path)
        
        if frame is None:
            logger.warning(f"Failed to load image: {image_path}")
            frame = np.zeros((self.height, self.width, 3), dtype=np.uint8)
        
        # Ensure consistent dimensions
        frame = cv2.resize(frame, (self.width, self.height))
        
        # Encode to JPEG
        ret, jpeg = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
        if not ret:
            return None, 0, 0
        
        frame_data = jpeg.tobytes()
        
        # Move to next image
        self.current_index += 1
        
        # Check if we've cycled through all images
        if self.current_index >= len(self.image_paths):
            if self.loop:
                logger.info("Image sequence reached end, looping to start")
                self.current_index = 0
            else:
                logger.info("Image sequence reached end (no looping)")
                self.is_finished = True
        
        return frame_data, self.width, self.height
    
    def cleanup(self):
        """Clean up resources."""
        pass


class SolidColorProducer(FrameProducer):
    """Producer that generates a solid color frame with text."""
    
    def __init__(self, width=640, height=480, fps=10):
        super().__init__(fps)
        self.width = width
        self.height = height
        self.frame_count = 0
        logger.info(f"Solid color mode: {self.width}x{self.height} @ {self.fps} FPS")
    
    def get_next_frame(self):
        """Generate a test frame."""
        frame = np.ones((self.height, self.width, 3), dtype=np.uint8) * 128
        
        # Add some visual elements
        cv2.putText(
            frame,
            "µStreamer Emulator",
            (50, self.height // 2 - 20),
            cv2.FONT_HERSHEY_SIMPLEX,
            1.5,
            (255, 255, 255),
            2
        )
        
        cv2.putText(
            frame,
            f"Frame: {self.frame_count}",
            (50, self.height // 2 + 40),
            cv2.FONT_HERSHEY_SIMPLEX,
            1,
            (255, 255, 255),
            2
        )
        
        # Encode to JPEG
        ret, jpeg = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
        if not ret:
            return None, 0, 0
        
        frame_data = jpeg.tobytes()
        self.frame_count += 1
        return frame_data, self.width, self.height
    
    def cleanup(self):
        """Clean up resources."""
        pass


def frame_producer_thread(producer, frame_buffer, fps):
    """Thread function that continuously produces frames."""
    frame_interval = 1.0 / fps
    next_frame_time = time.time()
    
    try:
        while True:
            current_time = time.time()
            
            if current_time >= next_frame_time:
                frame_data, width, height = producer.get_next_frame()
                
                if frame_data is not None:
                    frame_buffer.update_frame(frame_data, width, height)
                
                next_frame_time = current_time + frame_interval
            
            # Sleep briefly to avoid spinning
            time.sleep(0.001)
    
    except KeyboardInterrupt:
        pass
    finally:
        producer.cleanup()


def main():
    parser = argparse.ArgumentParser(
        description='MJPEG Stream Emulator for ustreamer testing'
    )
    
    source_group = parser.add_mutually_exclusive_group()
    source_group.add_argument(
        '--video',
        help='Path to video file to stream'
    )
    source_group.add_argument(
        '--images',
        nargs='+',
        help='Paths or glob patterns to images'
    )
    source_group.add_argument(
        '--test',
        action='store_true',
        help='Use test pattern (solid color with frame counter)'
    )
    
    parser.add_argument(
        '--fps',
        type=float,
        default=30,
        help='Frames per second (default: 30)'
    )
    
    parser.add_argument(
        '--no-loop',
        action='store_true',
        help='Play video/images once without looping (default: looping enabled)'
    )
    
    parser.add_argument(
        '--host',
        default='127.0.0.1',
        help='Host to bind to (default: 127.0.0.1)'
    )
    
    parser.add_argument(
        '--port',
        type=int,
        default=8080,
        help='Port to bind to (default: 8080)'
    )
    
    parser.add_argument(
        '--width',
        type=int,
        default=640,
        help='Frame width for test pattern (default: 640)'
    )
    
    parser.add_argument(
        '--height',
        type=int,
        default=480,
        help='Frame height for test pattern (default: 480)'
    )
    
    args = parser.parse_args()
    
    # Create frame buffer
    frame_buffer = FrameBuffer()
    MJPEGHandler.frame_buffer = frame_buffer
    
    # Determine looping behavior
    loop = not args.no_loop
    
    # Create frame producer
    producer = None
    try:
        if args.video:
            producer = VideoFileProducer(args.video, args.fps, loop=loop)
        elif args.images:
            # Expand glob patterns
            from glob import glob
            image_paths = []
            for pattern in args.images:
                image_paths.extend(glob(pattern))
            producer = ImageSequenceProducer(image_paths, args.fps, loop=loop)
        else:
            # Default to test pattern
            producer = SolidColorProducer(args.width, args.height, args.fps)
    except Exception as e:
        logger.error(f"Error creating frame producer: {e}")
        return 1
    
    # Start frame producer thread
    producer_thread = threading.Thread(
        target=frame_producer_thread,
        args=(producer, frame_buffer, producer.fps),
        daemon=True
    )
    producer_thread.start()
    
    # Start HTTP server
    server_address = (args.host, args.port)
    httpd = HTTPServer(server_address, MJPEGHandler)
    
    logger.info(f"Starting MJPEG server at http://{args.host}:{args.port}/")
    logger.info(f"Stream URL: http://{args.host}:{args.port}/stream")
    logger.info(f"Web interface: http://{args.host}:{args.port}/")
    logger.info("Press Ctrl+C to stop")
    
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        httpd.shutdown()
        producer.cleanup()
    
    return 0


if __name__ == '__main__':
    exit(main())
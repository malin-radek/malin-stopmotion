package com.stopmotion

import android.Manifest
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.os.Bundle
import android.util.Log
import android.widget.Button
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import androidx.camera.core.*
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.camera.view.PreviewView
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import java.io.ByteArrayOutputStream
import java.io.OutputStream
import java.net.ServerSocket
import java.net.Socket
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicReference

class MainActivity : AppCompatActivity() {

    companion object {
        private const val TAG = "StopMotionCamera"
        private const val PORT = 8081
        private const val REQUEST_CAMERA = 100
    }

    private lateinit var viewFinder: PreviewView
    private lateinit var statusText: TextView
    private lateinit var cameraExecutor: ExecutorService

    // Shared latest JPEG frame (thread-safe)
    private val latestFrame = AtomicReference<ByteArray?>(null)

    private var serverThread: Thread? = null
    private var isServerRunning = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        viewFinder = findViewById(R.id.viewFinder)
        statusText = findViewById(R.id.statusText)

        cameraExecutor = Executors.newSingleThreadExecutor()

        if (allPermissionsGranted()) {
            startCamera()
            startMjpegServer()
        } else {
            ActivityCompat.requestPermissions(this, arrayOf(Manifest.permission.CAMERA), REQUEST_CAMERA)
        }

        // Show local IP
        val ip = getLocalIpAddress()
        statusText.text = "Stream: http://$ip:$PORT/stream"
    }

    private fun startCamera() {
        val cameraProviderFuture = ProcessCameraProvider.getInstance(this)

        cameraProviderFuture.addListener({
            val cameraProvider = cameraProviderFuture.get()

            val preview = Preview.Builder().build().also {
                it.setSurfaceProvider(viewFinder.surfaceProvider)
            }

            // ImageAnalysis gives us frames as YUV → we convert to JPEG
            val imageAnalysis = ImageAnalysis.Builder()
                .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
                .build()

            imageAnalysis.setAnalyzer(cameraExecutor) { imageProxy ->
                val bitmap = imageProxy.toBitmap()
                val jpeg = bitmapToJpeg(bitmap, quality = 70)
                latestFrame.set(jpeg)
                imageProxy.close()
            }

            val cameraSelector = CameraSelector.DEFAULT_BACK_CAMERA

            try {
                cameraProvider.unbindAll()
                cameraProvider.bindToLifecycle(this, cameraSelector, preview, imageAnalysis)
            } catch (e: Exception) {
                Log.e(TAG, "Camera bind failed: ${e.message}")
            }
        }, ContextCompat.getMainExecutor(this))
    }

    private fun startMjpegServer() {
        isServerRunning = true
        serverThread = Thread {
            try {
                val serverSocket = ServerSocket(PORT)
                Log.i(TAG, "MJPEG server listening on port $PORT")

                while (isServerRunning) {
                    val client = serverSocket.accept()
                    // Each client in its own thread
                    Thread { handleClient(client) }.also { it.isDaemon = true }.start()
                }
                serverSocket.close()
            } catch (e: Exception) {
                Log.e(TAG, "Server error: ${e.message}")
            }
        }.also { it.isDaemon = true; it.start() }
    }

    private fun handleClient(socket: Socket) {
        try {
            val input = socket.getInputStream().bufferedReader()
            val output = socket.getOutputStream()

            // Read HTTP request line
            val requestLine = input.readLine() ?: return
            Log.d(TAG, "Request: $requestLine")

            when {
                requestLine.startsWith("GET /stream") -> serveStream(output)
                requestLine.startsWith("GET /frame") -> serveFrame(output)
                else -> send404(output)
            }
        } catch (e: Exception) {
            Log.d(TAG, "Client disconnected: ${e.message}")
        } finally {
            socket.close()
        }
    }

    private fun serveStream(output: OutputStream) {
        val boundary = "frame"
        val header = buildString {
            append("HTTP/1.1 200 OK\r\n")
            append("Content-Type: multipart/x-mixed-replace; boundary=$boundary\r\n")
            append("Cache-Control: no-cache\r\n")
            append("Connection: keep-alive\r\n")
            append("Access-Control-Allow-Origin: *\r\n")
            append("\r\n")
        }
        output.write(header.toByteArray())

        // Stream until client disconnects
        while (true) {
            val jpeg = latestFrame.get() ?: run {
                Thread.sleep(33) // ~30fps idle wait
                continue
            }

            val frameHeader = buildString {
                append("--$boundary\r\n")
                append("Content-Type: image/jpeg\r\n")
                append("Content-Length: ${jpeg.size}\r\n")
                append("\r\n")
            }

            output.write(frameHeader.toByteArray())
            output.write(jpeg)
            output.write("\r\n".toByteArray())
            output.flush()

            Thread.sleep(33) // ~30fps
        }
    }

    private fun serveFrame(output: OutputStream) {
        val jpeg = latestFrame.get()
        if (jpeg == null) {
            send503(output)
            return
        }
        val response = buildString {
            append("HTTP/1.1 200 OK\r\n")
            append("Content-Type: image/jpeg\r\n")
            append("Content-Length: ${jpeg.size}\r\n")
            append("Access-Control-Allow-Origin: *\r\n")
            append("Cache-Control: no-cache\r\n")
            append("\r\n")
        }
        output.write(response.toByteArray())
        output.write(jpeg)
        output.flush()
    }

    private fun send404(output: OutputStream) {
        output.write("HTTP/1.1 404 Not Found\r\n\r\nNot Found".toByteArray())
    }

    private fun send503(output: OutputStream) {
        output.write("HTTP/1.1 503 Service Unavailable\r\n\r\nNo frame available".toByteArray())
    }

    private fun bitmapToJpeg(bitmap: Bitmap, quality: Int = 70): ByteArray {
        val stream = ByteArrayOutputStream()
        bitmap.compress(Bitmap.CompressFormat.JPEG, quality, stream)
        return stream.toByteArray()
    }

    private fun getLocalIpAddress(): String {
        return try {
            val interfaces = java.net.NetworkInterface.getNetworkInterfaces()
            for (iface in interfaces) {
                for (addr in iface.inetAddresses) {
                    if (!addr.isLoopbackAddress && addr is java.net.Inet4Address) {
                        return addr.hostAddress ?: "unknown"
                    }
                }
            }
            "unknown"
        } catch (e: Exception) {
            "unknown"
        }
    }

    private fun allPermissionsGranted() =
        ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == REQUEST_CAMERA && grantResults.isNotEmpty() && grantResults[0] == PackageManager.PERMISSION_GRANTED) {
            startCamera()
            startMjpegServer()
        }
    }

    override fun onDestroy() {
        super.onDestroy()
        isServerRunning = false
        cameraExecutor.shutdown()
    }
}

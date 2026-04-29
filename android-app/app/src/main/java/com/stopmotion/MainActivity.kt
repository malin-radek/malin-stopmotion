package com.stopmotion

import android.Manifest
import android.app.AlertDialog
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Matrix
import android.os.Bundle
import android.util.Base64
import android.util.Log
import android.view.View
import android.widget.ArrayAdapter
import android.widget.Button
import android.widget.EditText
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.Spinner
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.ImageCapture
import androidx.camera.core.ImageCaptureException
import androidx.camera.core.ImageProxy
import androidx.camera.core.Preview
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.camera.view.PreviewView
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import org.json.JSONArray
import org.json.JSONObject
import com.google.zxing.BinaryBitmap
import com.google.zxing.MultiFormatReader
import com.google.zxing.Result
import com.google.zxing.client.android.Intents
import com.google.zxing.common.HybridBinarizer
import java.util.EnumMap
import java.util.EnumSet
import java.io.ByteArrayOutputStream
import java.io.File
import java.io.OutputStream
import java.net.HttpURLConnection
import java.net.ServerSocket
import java.net.Socket
import java.net.URL
import java.util.UUID
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import java.util.concurrent.ScheduledExecutorService
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference

class MainActivity : AppCompatActivity() {

    companion object {
        private const val TAG = "StopMotionCamera"
        private const val PORT = 8081
        private const val REQUEST_CAMERA = 100
    }

    data class FrameRecord(
        val localId: String,
        val timestamp: Long,
        var serverId: String? = null,
        var synced: Boolean = false,
        var deleted: Boolean = false
    )

    data class ProjectRecord(
        val localId: String,
        var serverId: String?,
        var name: String,
        var fps: Int,
        var resolution: String,
        var orientation: String,
        val frames: MutableList<FrameRecord>
    )

    private lateinit var viewFinder: PreviewView
    private lateinit var onionImage: ImageView
    private lateinit var statusText: TextView
    private lateinit var projectSpinner: Spinner
    private lateinit var backendInput: EditText
    private lateinit var tokenInput: EditText
    private lateinit var cameraExecutor: ExecutorService
    private lateinit var scheduledExecutor: ScheduledExecutorService

    private var imageCapture: ImageCapture? = null
    private val latestFrame = AtomicReference<ByteArray?>(null)
    private var serverThread: Thread? = null
    private var isServerRunning = false
    private var lastScannedQrCode: String? = null
    private var lastQrScanTime: Long = 0

    private val projects = mutableListOf<ProjectRecord>()
    private var currentProjectIndex = 0
    private var onionIndex = -1

    private val prefs by lazy { getSharedPreferences("stopmotion", MODE_PRIVATE) }
    private val deviceId by lazy {
        prefs.getString("device_id", null) ?: UUID.randomUUID().toString().also {
            prefs.edit().putString("device_id", it).apply()
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        viewFinder = findViewById(R.id.viewFinder)
        onionImage = findViewById(R.id.onionImage)
        statusText = findViewById(R.id.statusText)
        projectSpinner = findViewById(R.id.projectSpinner)
        backendInput = findViewById(R.id.backendInput)
        tokenInput = findViewById(R.id.tokenInput)
        cameraExecutor = Executors.newSingleThreadExecutor()
        scheduledExecutor = Executors.newScheduledThreadPool(1)

        backendInput.setText(prefs.getString("backend_url", "http://192.168.1.10:8000"))

        loadProjects()
        ensureAtLeastOneProject()
        bindUi()
        handlePairIntent(intent)

        if (allPermissionsGranted()) {
            startCamera()
            startMjpegServer()
        } else {
            ActivityCompat.requestPermissions(this, arrayOf(Manifest.permission.CAMERA), REQUEST_CAMERA)
        }
        refreshUi()

        // Start automatic sync every 30 seconds
        scheduledExecutor.scheduleAtFixedRate({
            if (prefs.getString("account_id", null) != null) {
                syncNow()
            }
        }, 30, 30, TimeUnit.SECONDS)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handlePairIntent(intent)
    }

    private fun bindUi() {
        findViewById<Button>(R.id.captureButton).setOnClickListener { captureFrame() }
        findViewById<Button>(R.id.syncButton).setOnClickListener { syncNow() }
        findViewById<Button>(R.id.pairButton).setOnClickListener { claimPairing(tokenInput.text.toString().trim()) }
        findViewById<Button>(R.id.newProjectButton).setOnClickListener { promptNewProject() }
        findViewById<Button>(R.id.prevButton).setOnClickListener { moveOnion(-1) }
        findViewById<Button>(R.id.nextButton).setOnClickListener { moveOnion(1) }
        findViewById<Button>(R.id.deleteButton).setOnClickListener { deleteCurrentFrame(false) }
        findViewById<Button>(R.id.deleteTailButton).setOnClickListener { deleteCurrentFrame(true) }
        projectSpinner.setOnItemSelectedListener(object : android.widget.AdapterView.OnItemSelectedListener {
            override fun onItemSelected(parent: android.widget.AdapterView<*>?, view: View?, position: Int, id: Long) {
                currentProjectIndex = position
                onionIndex = activeProject().frames.indexOfLast { !it.deleted }
                refreshOnion()
                updateStatus()
            }
            override fun onNothingSelected(parent: android.widget.AdapterView<*>?) = Unit
        })
    }

    private fun startCamera() {
        val cameraProviderFuture = ProcessCameraProvider.getInstance(this)
        cameraProviderFuture.addListener({
            val cameraProvider = cameraProviderFuture.get()
            val preview = Preview.Builder().build().also {
                it.setSurfaceProvider(viewFinder.surfaceProvider)
            }
            imageCapture = ImageCapture.Builder()
                .setCaptureMode(ImageCapture.CAPTURE_MODE_MINIMIZE_LATENCY)
                .build()
            val imageAnalysis = ImageAnalysis.Builder()
                .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
                .build()
            
            val qrReader = MultiFormatReader()
            
            imageAnalysis.setAnalyzer(cameraExecutor) { proxy ->
                try {
                    val bitmap = proxy.toBitmap()
                    latestFrame.set(bitmapToJpeg(bitmap, 70))
                    
                    // QR scanning with ZXing
                    try {
                        val intArray = IntArray(bitmap.width * bitmap.height)
                        bitmap.getPixels(intArray, 0, bitmap.width, 0, 0, bitmap.width, bitmap.height)
                        val luminanceSource = object : com.google.zxing.LuminanceSource(bitmap.width, bitmap.height) {
                            override fun getRow(y: Int, row: ByteArray?): ByteArray {
                                val result = row ?: ByteArray(width)
                                for (x in 0 until width) {
                                    val pixel = intArray[y * width + x]
                                    val r = (pixel shr 16) and 0xFF
                                    val g = (pixel shr 8) and 0xFF
                                    val b = pixel and 0xFF
                                    result[x] = (((r + g + b) / 3) and 0xFF).toByte()
                                }
                                return result
                            }

                            override fun getMatrix(): ByteArray {
                                val matrix = ByteArray(width * height)
                                for (y in 0 until height) {
                                    for (x in 0 until width) {
                                        val pixel = intArray[y * width + x]
                                        val r = (pixel shr 16) and 0xFF
                                        val g = (pixel shr 8) and 0xFF
                                        val b = pixel and 0xFF
                                        matrix[y * width + x] = (((r + g + b) / 3) and 0xFF).toByte()
                                    }
                                }
                                return matrix
                            }

                            override fun isCropSupported(): Boolean = false

                            override fun crop(left: Int, top: Int, width: Int, height: Int): com.google.zxing.LuminanceSource {
                                return this
                            }
                        }
                        
                        val binaryBitmap = BinaryBitmap(HybridBinarizer(luminanceSource))
                        val result = qrReader.decodeWithState(binaryBitmap)
                        val qrValue = result.text
                        if (qrValue != null && qrValue != lastScannedQrCode && System.currentTimeMillis() - lastQrScanTime > 2000) {
                            lastScannedQrCode = qrValue
                            lastQrScanTime = System.currentTimeMillis()
                            if (qrValue.startsWith("stopmotion://pair")) {
                                val token = qrValue.substringAfterLast("token=")
                                if (token.isNotBlank()) {
                                    runOnUiThread {
                                        tokenInput.setText(token)
                                        claimPairing(token)
                                    }
                                }
                            }
                        }
                        qrReader.reset()
                    } catch (_: Exception) {
                        // QR not found in frame
                    }
                } catch (e: Exception) {
                    Log.d(TAG, "Analysis frame failed: ${e.message}")
                } finally {
                    proxy.close()
                }
            }
            try {
                cameraProvider.unbindAll()
                cameraProvider.bindToLifecycle(
                    this,
                    CameraSelector.DEFAULT_BACK_CAMERA,
                    preview,
                    imageCapture,
                    imageAnalysis
                )
            } catch (e: Exception) {
                Log.e(TAG, "Camera bind failed: ${e.message}")
            }
        }, ContextCompat.getMainExecutor(this))
    }

    private fun captureFrame() {
        val capture = imageCapture ?: return
        capture.takePicture(cameraExecutor, object : ImageCapture.OnImageCapturedCallback() {
            override fun onCaptureSuccess(image: ImageProxy) {
                try {
                    val bitmap = image.toBitmap()
                    val project = activeProject()
                    if (project.orientation == "landscape" && bitmap.height > bitmap.width) {
                        runOnUiThread {
                            Toast.makeText(this@MainActivity, "Projekt jest poziomy, a telefon trzymasz pionowo.", Toast.LENGTH_LONG).show()
                        }
                    }
                    val frame = FrameRecord(UUID.randomUUID().toString(), System.currentTimeMillis())
                    frameFile(project, frame).writeBytes(bitmapToJpeg(bitmap, 92))
                    thumbFile(project, frame).writeBytes(bitmapToJpeg(scaleBitmap(bitmap, 320), 72))
                    project.frames.add(frame)
                    onionIndex = project.frames.lastIndex
                    saveProjects()
                    runOnUiThread {
                        refreshOnion()
                        updateStatus()
                    }
                } finally {
                    image.close()
                }
            }

            override fun onError(exception: ImageCaptureException) {
                runOnUiThread { toast("Nie udalo sie zapisac klatki: ${exception.message}") }
            }
        })
    }

    private fun syncNow() {
        prefs.edit().putString("backend_url", backendInput.text.toString().trim()).apply()
        Thread {
            try {
                val accountId = prefs.getString("account_id", null)
                    ?: throw IllegalStateException("Najpierw sparuj telefon z kontem.")
                val body = JSONObject()
                    .put("account_id", accountId)
                    .put("device_id", deviceId)
                    .put("projects", JSONArray(projects.map { projectToJsonForSync(it) }))
                val response = postJson("${backendUrl()}/mobile/sync", body)
                applySyncResponse(response)
                saveProjects()
                runOnUiThread {
                    refreshUi()
                    toast("Synchronizacja zakonczona")
                }
            } catch (e: Exception) {
                runOnUiThread { toast("Sync offline: ${e.message}") }
            }
        }.start()
    }

    private fun claimPairing(token: String) {
        if (token.isBlank()) {
            toast("Wklej token z QR albo otworz link stopmotion://pair z kodu QR.")
            return
        }
        prefs.edit().putString("backend_url", backendInput.text.toString().trim()).apply()
        Thread {
            try {
                val body = JSONObject()
                    .put("token", token)
                    .put("device_name", android.os.Build.MODEL ?: "Telefon")
                    .put("device_id", deviceId)
                val response = postJson("${backendUrl()}/pairings/claim", body)
                val account = response.getJSONObject("account")
                prefs.edit().putString("account_id", account.getString("id")).apply()
                importServerProjects(response.optJSONArray("projects") ?: JSONArray())
                saveProjects()
                runOnUiThread {
                    refreshUi()
                    toast("Sparowano z kontem ${account.optString("name")}")
                }
                syncNow()
            } catch (e: Exception) {
                runOnUiThread { toast("Parowanie nieudane: ${e.message}") }
            }
        }.start()
    }

    private fun handlePairIntent(intent: Intent?) {
        val token = intent?.data?.getQueryParameter("token") ?: return
        tokenInput.setText(token)
        claimPairing(token)
    }

    private fun promptNewProject() {
        val layout = LinearLayout(this)
        layout.orientation = LinearLayout.VERTICAL
        layout.setPadding(32, 8, 32, 0)
        val input = EditText(this)
        input.hint = "Nazwa projektu"
        val resolution = Spinner(this)
        resolution.adapter = ArrayAdapter(this, android.R.layout.simple_spinner_dropdown_item, listOf("1920x1080", "1280x720", "3840x2160", "1080x1920"))
        val orientation = Spinner(this)
        orientation.adapter = ArrayAdapter(this, android.R.layout.simple_spinner_dropdown_item, listOf("landscape", "portrait"))
        layout.addView(input)
        layout.addView(resolution)
        layout.addView(orientation)
        AlertDialog.Builder(this)
            .setTitle("Nowy projekt")
            .setView(layout)
            .setNegativeButton("Anuluj", null)
            .setPositiveButton("Utworz") { _, _ ->
                val name = input.text.toString().trim().ifBlank { "Projekt ${projects.size + 1}" }
                projects.add(ProjectRecord(
                    UUID.randomUUID().toString(),
                    null,
                    name,
                    12,
                    resolution.selectedItem.toString(),
                    orientation.selectedItem.toString(),
                    mutableListOf()
                ))
                currentProjectIndex = projects.lastIndex
                onionIndex = -1
                saveProjects()
                refreshUi()
            }
            .show()
    }

    private fun deleteCurrentFrame(deleteTail: Boolean) {
        val project = activeProject()
        if (project.frames.isEmpty() || onionIndex !in project.frames.indices) return
        if (deleteTail) {
            for (i in onionIndex until project.frames.size) project.frames[i].deleted = true
        } else {
            project.frames[onionIndex].deleted = true
        }
        while (onionIndex >= 0 && project.frames.getOrNull(onionIndex)?.deleted == true) onionIndex--
        saveProjects()
        refreshOnion()
        updateStatus()
    }

    private fun moveOnion(delta: Int) {
        val visible = activeProject().frames.withIndex().filter { !it.value.deleted }.map { it.index }
        if (visible.isEmpty()) {
            onionIndex = -1
        } else {
            val currentVisible = visible.indexOf(onionIndex).let { if (it < 0) visible.lastIndex else it }
            onionIndex = visible[(currentVisible + delta).coerceIn(0, visible.lastIndex)]
        }
        refreshOnion()
        updateStatus()
    }

    private fun refreshUi() {
        val adapter = ArrayAdapter(this, android.R.layout.simple_spinner_dropdown_item, projects.map { it.name })
        projectSpinner.adapter = adapter
        projectSpinner.setSelection(currentProjectIndex.coerceIn(0, projects.lastIndex))
        refreshOnion()
        updateStatus()
    }

    private fun refreshOnion() {
        val project = activeProject()
        val frame = project.frames.getOrNull(onionIndex)
        val frameFileObj = if (frame != null && !frame.deleted) frameFile(project, frame) else null
        if (frameFileObj != null && frameFileObj.exists()) {
            var bitmap = BitmapFactory.decodeFile(frameFileObj.absolutePath)

            // Rotate bitmap if project orientation is portrait
            if (project.orientation == "portrait" && bitmap.width > bitmap.height) {
                val matrix = Matrix()
                matrix.postRotate(90f)
                bitmap = Bitmap.createBitmap(bitmap, 0, 0, bitmap.width, bitmap.height, matrix, true)
            } else if (project.orientation == "landscape" && bitmap.width < bitmap.height) {
                val matrix = Matrix()
                matrix.postRotate(-90f)
                bitmap = Bitmap.createBitmap(bitmap, 0, 0, bitmap.width, bitmap.height, matrix, true)
            }

            onionImage.setImageBitmap(bitmap)
            onionImage.visibility = View.VISIBLE
            onionImage.scaleType = ImageView.ScaleType.CENTER_CROP
        } else {
            onionImage.setImageDrawable(null)
            onionImage.visibility = View.GONE
        }
    }

    private fun updateStatus() {
        val project = activeProject()
        val count = project.frames.count { !it.deleted }
        val unsynced = project.frames.count { !it.synced || it.deleted }
        val account = prefs.getString("account_id", "brak konta")
        statusText.text = "Konto: $account | ${project.name} | klatki: $count | do sync: $unsynced | onion: ${onionIndex + 1}"
    }

    private fun projectToJsonForSync(project: ProjectRecord): JSONObject {
        val frames = JSONArray()
        for (frame in project.frames) {
            if (frame.synced && !frame.deleted) continue
            val item = JSONObject()
                .put("local_id", frame.localId)
                .put("timestamp", frame.timestamp)
                .put("deleted", frame.deleted)
            if (!frame.deleted) {
                item.put("jpeg_base64", Base64.encodeToString(frameFile(project, frame).readBytes(), Base64.NO_WRAP))
            }
            frames.put(item)
        }
        return JSONObject()
            .put("local_id", project.localId)
            .put("server_id", project.serverId)
            .put("name", project.name)
            .put("fps", project.fps)
            .put("resolution", project.resolution)
            .put("orientation", project.orientation)
            .put("frames", frames)
    }

    private fun applySyncResponse(response: JSONObject) {
        val syncedProjects = response.optJSONArray("projects") ?: JSONArray()
        for (i in 0 until syncedProjects.length()) {
            val item = syncedProjects.getJSONObject(i)
            val project = projects.firstOrNull { it.localId == item.getString("local_id") } ?: continue
            project.serverId = item.getString("server_id")
            val syncedLocalIds = mutableSetOf<String>()
            val frames = item.optJSONArray("frames") ?: JSONArray()
            for (j in 0 until frames.length()) {
                val remote = frames.getJSONObject(j)
                val localId = remote.optString("local_id", "")
                if (localId.isBlank()) continue
                syncedLocalIds.add(localId)
                val local = project.frames.firstOrNull { it.localId == localId }
                if (local != null) {
                    local.serverId = remote.optString("id")
                    local.synced = true
                    local.deleted = false
                    val thumb64 = remote.optString("thumbnail_base64", "")
                    if (thumb64.isNotBlank()) thumbFile(project, local).writeBytes(Base64.decode(thumb64, Base64.DEFAULT))
                }
            }
            val deleted = item.optJSONArray("deleted_frame_ids") ?: JSONArray()
            for (j in 0 until deleted.length()) syncedLocalIds.add(deleted.getString(j))
            project.frames.removeAll { it.deleted && syncedLocalIds.contains(it.localId) }
        }
    }

    private fun importServerProjects(serverProjects: JSONArray) {
        for (i in 0 until serverProjects.length()) {
            val remote = serverProjects.getJSONObject(i)
            val serverId = remote.getString("id")
            if (projects.none { it.serverId == serverId }) {
                projects.add(
                    ProjectRecord(
                        UUID.randomUUID().toString(),
                        serverId,
                        remote.optString("name", serverId),
                        remote.optInt("fps", 12),
                        remote.optString("resolution", "1920x1080"),
                        remote.optString("orientation", "landscape"),
                        mutableListOf()
                    )
                )
            }
        }
    }

    private fun postJson(url: String, body: JSONObject): JSONObject {
        val bytes = body.toString().toByteArray(Charsets.UTF_8)
        val conn = (URL(url).openConnection() as HttpURLConnection).apply {
            requestMethod = "POST"
            connectTimeout = 4000
            readTimeout = 20000
            doOutput = true
            setRequestProperty("Content-Type", "application/json")
            setRequestProperty("Content-Length", bytes.size.toString())
        }
        conn.outputStream.use { it.write(bytes) }
        val stream = if (conn.responseCode in 200..299) conn.inputStream else conn.errorStream
        val text = stream.bufferedReader().readText()
        if (conn.responseCode !in 200..299) throw IllegalStateException(text)
        return JSONObject(text)
    }

    private fun loadProjects() {
        projects.clear()
        val file = File(filesDir, "projects.json")
        if (!file.exists()) return
        val arr = JSONArray(file.readText())
        for (i in 0 until arr.length()) {
            val item = arr.getJSONObject(i)
            val frames = mutableListOf<FrameRecord>()
            val frameArr = item.optJSONArray("frames") ?: JSONArray()
            for (j in 0 until frameArr.length()) {
                val f = frameArr.getJSONObject(j)
                frames.add(FrameRecord(
                    f.getString("local_id"),
                    f.getLong("timestamp"),
                    f.optString("server_id").ifBlank { null },
                    f.optBoolean("synced", false),
                    f.optBoolean("deleted", false)
                ))
            }
            projects.add(ProjectRecord(
                item.getString("local_id"),
                item.optString("server_id").ifBlank { null },
                item.optString("name", "Projekt"),
                item.optInt("fps", 12),
                item.optString("resolution", "1920x1080"),
                item.optString("orientation", "landscape"),
                frames
            ))
        }
    }

    private fun saveProjects() {
        val arr = JSONArray()
        for (project in projects) {
            val frames = JSONArray()
            for (frame in project.frames) {
                frames.put(JSONObject()
                    .put("local_id", frame.localId)
                    .put("server_id", frame.serverId)
                    .put("timestamp", frame.timestamp)
                    .put("synced", frame.synced)
                    .put("deleted", frame.deleted))
            }
            arr.put(JSONObject()
                .put("local_id", project.localId)
                .put("server_id", project.serverId)
                .put("name", project.name)
                .put("fps", project.fps)
                .put("resolution", project.resolution)
                .put("orientation", project.orientation)
                .put("frames", frames))
        }
        File(filesDir, "projects.json").writeText(arr.toString())
    }

    private fun ensureAtLeastOneProject() {
        if (projects.isEmpty()) {
            projects.add(ProjectRecord(UUID.randomUUID().toString(), null, "Telefon offline", 12, "1920x1080", "landscape", mutableListOf()))
            saveProjects()
        }
    }

    private fun activeProject(): ProjectRecord = projects[currentProjectIndex.coerceIn(0, projects.lastIndex)]

    private fun projectDir(project: ProjectRecord): File = File(filesDir, "projects/${project.localId}").also { it.mkdirs() }
    private fun frameFile(project: ProjectRecord, frame: FrameRecord): File = File(projectDir(project), "${frame.localId}.jpg")
    private fun thumbFile(project: ProjectRecord, frame: FrameRecord): File = File(projectDir(project), "${frame.localId}.thumb.jpg")
    private fun backendUrl(): String = backendInput.text.toString().trim().trimEnd('/')

    private fun scaleBitmap(bitmap: Bitmap, targetWidth: Int): Bitmap {
        if (bitmap.width <= targetWidth) return bitmap
        val h = (bitmap.height * (targetWidth.toFloat() / bitmap.width)).toInt()
        return Bitmap.createScaledBitmap(bitmap, targetWidth, h, true)
    }

    private fun bitmapToJpeg(bitmap: Bitmap, quality: Int): ByteArray {
        val stream = ByteArrayOutputStream()
        bitmap.compress(Bitmap.CompressFormat.JPEG, quality, stream)
        return stream.toByteArray()
    }

    private fun toast(message: String) = Toast.makeText(this, message, Toast.LENGTH_LONG).show()

    private fun startMjpegServer() {
        isServerRunning = true
        serverThread = Thread {
            try {
                val serverSocket = ServerSocket(PORT)
                while (isServerRunning) {
                    val client = serverSocket.accept()
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
            val requestLine = input.readLine() ?: return
            when {
                requestLine.startsWith("GET /stream") -> serveStream(output)
                requestLine.startsWith("GET /frame") -> serveFrame(output)
                else -> output.write("HTTP/1.1 404 Not Found\r\n\r\nNot Found".toByteArray())
            }
        } catch (_: Exception) {
        } finally {
            socket.close()
        }
    }

    private fun serveStream(output: OutputStream) {
        output.write(("HTTP/1.1 200 OK\r\nContent-Type: multipart/x-mixed-replace; boundary=frame\r\n" +
            "Cache-Control: no-cache\r\nConnection: keep-alive\r\nAccess-Control-Allow-Origin: *\r\n\r\n").toByteArray())
        while (true) {
            val jpeg = latestFrame.get()
            if (jpeg == null) {
                Thread.sleep(50)
                continue
            }
            output.write("--frame\r\nContent-Type: image/jpeg\r\nContent-Length: ${jpeg.size}\r\n\r\n".toByteArray())
            output.write(jpeg)
            output.write("\r\n".toByteArray())
            output.flush()
            Thread.sleep(50)
        }
    }

    private fun serveFrame(output: OutputStream) {
        val jpeg = latestFrame.get()
        if (jpeg == null) {
            output.write("HTTP/1.1 503 Service Unavailable\r\n\r\nNo frame".toByteArray())
            return
        }
        output.write("HTTP/1.1 200 OK\r\nContent-Type: image/jpeg\r\nContent-Length: ${jpeg.size}\r\nAccess-Control-Allow-Origin: *\r\n\r\n".toByteArray())
        output.write(jpeg)
        output.flush()
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
        scheduledExecutor.shutdown()
    }
}

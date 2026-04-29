package com.stopmotion

import android.content.Context
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.util.AttributeSet
import android.view.View
import kotlin.math.cos
import kotlin.math.sin
import kotlin.math.atan2

data class TrackPoint(val normX: Float, val normY: Float)

class TrackingOverlayView @JvmOverloads constructor(
    context: Context, attrs: AttributeSet? = null
) : View(context, attrs) {

    private val paintRed = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.RED; style = Paint.Style.STROKE; strokeWidth = 8f; strokeCap = Paint.Cap.ROUND
    }
    private val paintRedFill = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.RED; style = Paint.Style.FILL
    }
    private val paintYellow = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.YELLOW; style = Paint.Style.STROKE; strokeWidth = 7f; strokeCap = Paint.Cap.ROUND
    }
    private val paintYellowFill = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.argb(200, 255, 200, 0); style = Paint.Style.FILL
    }

    // Saved (onion) points – set from MainActivity
    var savedPoints: List<TrackPoint> = emptyList()
        set(value) { field = value; invalidate() }

    // Live camera tracked point – updated every frame
    var livePoint: TrackPoint? = null
        set(value) { field = value; invalidate() }

    var trackingEnabled: Boolean = false
        set(value) { field = value; invalidate() }

    override fun onDraw(canvas: Canvas) {
        if (!trackingEnabled) return
        val w = width.toFloat()
        val h = height.toFloat()

        // Draw all saved dots
        for (p in savedPoints) {
            canvas.drawCircle(p.normX * w, p.normY * h, 12f, paintYellowFill)
        }

        // Draw lines between consecutive saved points
        for (i in 0 until savedPoints.size - 1) {
            val a = savedPoints[i]; val b = savedPoints[i + 1]
            canvas.drawLine(a.normX * w, a.normY * h, b.normX * w, b.normY * h, paintYellow)
        }

        val live = livePoint ?: return

        val lx = live.normX * w
        val ly = live.normY * h

        // Arrow from last saved → live
        if (savedPoints.isNotEmpty()) {
            val last = savedPoints.last()
            val px = last.normX * w; val py = last.normY * h
            canvas.drawLine(px, py, lx, ly, paintYellow)
            drawArrowHead(canvas, px, py, lx, ly, 45f, paintYellow)
        }

        // Big red circle + crosshair at live point
        canvas.drawCircle(lx, ly, 44f, paintRed)
        canvas.drawLine(lx - 65f, ly, lx + 65f, ly, paintRed)
        canvas.drawLine(lx, ly - 65f, lx, ly + 65f, paintRed)
        canvas.drawCircle(lx, ly, 10f, paintRedFill)
    }

    private fun drawArrowHead(canvas: Canvas, x1: Float, y1: Float, x2: Float, y2: Float, headLen: Float, paint: Paint) {
        val angle = atan2((y2 - y1).toDouble(), (x2 - x1).toDouble())
        canvas.drawLine(x2, y2,
            (x2 - headLen * cos(angle - Math.PI / 6)).toFloat(),
            (y2 - headLen * sin(angle - Math.PI / 6)).toFloat(), paint)
        canvas.drawLine(x2, y2,
            (x2 - headLen * cos(angle + Math.PI / 6)).toFloat(),
            (y2 - headLen * sin(angle + Math.PI / 6)).toFloat(), paint)
    }
}


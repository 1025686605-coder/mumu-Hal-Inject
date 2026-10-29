package com.probe;

import android.app.Activity;
import android.hardware.Sensor;
import android.hardware.SensorEvent;
import android.hardware.SensorEventListener;
import android.hardware.SensorManager;
import android.os.Bundle;
import android.util.Log;

public class ProbeActivity extends Activity implements SensorEventListener {
    private static final String TAG = "SensorProbe";
    private static final int[] TYPES = new int[] {
        Sensor.TYPE_ACCELEROMETER, Sensor.TYPE_GYROSCOPE, Sensor.TYPE_ORIENTATION,
        Sensor.TYPE_ROTATION_VECTOR, Sensor.TYPE_GRAVITY, Sensor.TYPE_MAGNETIC_FIELD
    };
    private static final String[] NAMES = new String[] { "ACCEL","GYRO","ORIENT","ROT_VEC","GRAVITY","MAG" };
    private final long[] lastLog = new long[TYPES.length];
    private SensorManager sm;
    @Override protected void onCreate(Bundle b) {
        super.onCreate(b);
        sm = (SensorManager) getSystemService(SENSOR_SERVICE);
        for (int i = 0; i < TYPES.length; i++) {
            Sensor s = sm.getDefaultSensor(TYPES[i]);
            if (s == null) { Log.i(TAG, NAMES[i] + " NOT_PRESENT"); continue; }
            sm.registerListener(this, s, SensorManager.SENSOR_DELAY_GAME);
            Log.i(TAG, NAMES[i] + " vendor=" + s.getVendor());
        }
    }
    @Override public void onSensorChanged(SensorEvent e) {
        int type = e.sensor.getType(); int idx = -1;
        for (int i = 0; i < TYPES.length; i++) { if (TYPES[i] == type) { idx = i; break; } }
        if (idx < 0) return;
        long now = System.currentTimeMillis();
        if (now - lastLog[idx] < 0) return;
        lastLog[idx] = now;
        StringBuilder sb = new StringBuilder(NAMES[idx]);
        for (float v : e.values) sb.append(String.format(" %.3f", v));
        Log.i(TAG, sb.toString());
    }
    @Override public void onAccuracyChanged(Sensor s, int a) { }
}
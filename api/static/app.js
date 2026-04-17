// Three.js neural-particle hero scene.
//
// A drifting cloud of particles connected by short lines whenever any two
// particles fall within a threshold distance. Loose neuro/network theme,
// keeps memory and CPU low (no model loads, no shaders beyond the
// PointsMaterial built-in). Resizes with the viewport and pauses when
// the tab is hidden so it doesn't drain battery in the background.

import * as THREE from "three";

const canvas = document.getElementById("neural-bg");
if (canvas) {
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(60, 1, 0.1, 1000);
  camera.position.z = 60;

  const PARTICLE_COUNT = 140;
  const FIELD_SIZE = 80;
  const LINK_DISTANCE = 14;

  const positions = new Float32Array(PARTICLE_COUNT * 3);
  const velocities = new Float32Array(PARTICLE_COUNT * 3);
  for (let i = 0; i < PARTICLE_COUNT; i++) {
    positions[i * 3]     = (Math.random() - 0.5) * FIELD_SIZE;
    positions[i * 3 + 1] = (Math.random() - 0.5) * FIELD_SIZE;
    positions[i * 3 + 2] = (Math.random() - 0.5) * FIELD_SIZE * 0.6;
    velocities[i * 3]     = (Math.random() - 0.5) * 0.04;
    velocities[i * 3 + 1] = (Math.random() - 0.5) * 0.04;
    velocities[i * 3 + 2] = (Math.random() - 0.5) * 0.04;
  }

  const pGeom = new THREE.BufferGeometry();
  pGeom.setAttribute("position", new THREE.BufferAttribute(positions, 3));
  const pMat = new THREE.PointsMaterial({
    size: 1.4,
    color: 0x67e8f9,         // cyan-300
    transparent: true,
    opacity: 0.85,
    sizeAttenuation: true,
  });
  const points = new THREE.Points(pGeom, pMat);
  scene.add(points);

  // Pre-allocate the lines geometry to a generous upper bound so we don't
  // re-allocate buffers every frame. Capacity is the worst-case pair count.
  const MAX_LINKS = PARTICLE_COUNT * 8;
  const linePositions = new Float32Array(MAX_LINKS * 2 * 3);
  const lineGeom = new THREE.BufferGeometry();
  lineGeom.setAttribute("position", new THREE.BufferAttribute(linePositions, 3));
  const lineMat = new THREE.LineBasicMaterial({
    color: 0xe879f9,         // fuchsia-400
    transparent: true,
    opacity: 0.18,
  });
  const lines = new THREE.LineSegments(lineGeom, lineMat);
  scene.add(lines);

  function resize() {
    const w = window.innerWidth, h = window.innerHeight;
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
  }
  resize();
  window.addEventListener("resize", resize);

  let running = true;
  document.addEventListener("visibilitychange", () => {
    running = document.visibilityState === "visible";
    if (running) animate();
  });

  function animate() {
    if (!running) return;
    requestAnimationFrame(animate);

    // Drift particles, wrap around the field box.
    for (let i = 0; i < PARTICLE_COUNT; i++) {
      for (let axis = 0; axis < 3; axis++) {
        const idx = i * 3 + axis;
        positions[idx] += velocities[idx];
        const limit = axis === 2 ? FIELD_SIZE * 0.3 : FIELD_SIZE * 0.5;
        if (positions[idx] > limit) positions[idx] = -limit;
        else if (positions[idx] < -limit) positions[idx] = limit;
      }
    }
    pGeom.attributes.position.needsUpdate = true;

    // Recompute neighbor links. O(N²) but N=140 → ~20K dist checks/frame, fine.
    let written = 0;
    for (let i = 0; i < PARTICLE_COUNT; i++) {
      for (let j = i + 1; j < PARTICLE_COUNT; j++) {
        const dx = positions[i*3]   - positions[j*3];
        const dy = positions[i*3+1] - positions[j*3+1];
        const dz = positions[i*3+2] - positions[j*3+2];
        const d2 = dx*dx + dy*dy + dz*dz;
        if (d2 < LINK_DISTANCE * LINK_DISTANCE && written < MAX_LINKS) {
          const k = written * 6;
          linePositions[k]   = positions[i*3];
          linePositions[k+1] = positions[i*3+1];
          linePositions[k+2] = positions[i*3+2];
          linePositions[k+3] = positions[j*3];
          linePositions[k+4] = positions[j*3+1];
          linePositions[k+5] = positions[j*3+2];
          written++;
        }
      }
    }
    lineGeom.setDrawRange(0, written * 2);
    lineGeom.attributes.position.needsUpdate = true;

    points.rotation.y += 0.0008;
    lines.rotation.y += 0.0008;

    renderer.render(scene, camera);
  }
  animate();
}

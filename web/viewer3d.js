// Interactive 3D STL viewer (M7 follow-up). Uses the vendored Three.js (global
// THREE) + STLLoader + OrbitControls — no build step, works offline. Given a
// container and an STL URL, it fetches the mesh (optionally with the founder
// review token, so a pending design's STL can be viewed on the dashboard),
// frames it, and lets you orbit (drag), zoom (wheel) and pan (right-drag).
//
// Vulcan3D.create(container, stlUrl, { token, fallbackImg, onReady }) -> handle
//   handle.dispose()   tears down the renderer/animation
//   handle.resetView() re-frames the camera
window.Vulcan3D = (function () {
  function frame(camera, controls, object) {
    const box = new THREE.Box3().setFromObject(object);
    if (box.isEmpty()) return;
    const size = box.getSize(new THREE.Vector3());
    const center = box.getCenter(new THREE.Vector3());
    const maxDim = Math.max(size.x, size.y, size.z) || 1;
    const fov = (camera.fov * Math.PI) / 180;
    const dist = (maxDim / (2 * Math.tan(fov / 2))) * 1.7;
    camera.near = maxDim / 200;
    camera.far = maxDim * 200;
    camera.position.set(center.x + dist * 0.75, center.y + dist * 0.55, center.z + dist);
    camera.updateProjectionMatrix();
    controls.target.copy(center);
    controls.update();
  }

  function showFallback(container, opts, message) {
    container.innerHTML = "";
    if (opts.fallbackImg) {
      const img = document.createElement("img");
      img.src = opts.fallbackImg;
      img.alt = "Part preview";
      img.className = "viewer-fallback-img";
      container.appendChild(img);
    } else {
      const div = document.createElement("div");
      div.className = "viewer-fallback";
      div.textContent = message || "3D preview unavailable.";
      container.appendChild(div);
    }
  }

  async function create(container, stlUrl, opts = {}) {
    container.innerHTML = "";
    if (typeof THREE === "undefined" || !THREE.STLLoader || !THREE.OrbitControls) {
      showFallback(container, opts, "3D library failed to load.");
      return { dispose() {}, resetView() {} };
    }

    let geometry;
    try {
      const headers = opts.token ? { "X-Review-Token": opts.token } : {};
      const resp = await fetch(stlUrl, { headers });
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      geometry = new THREE.STLLoader().parse(await resp.arrayBuffer());
    } catch (err) {
      showFallback(container, opts, `3D preview unavailable (${err.message}).`);
      return { dispose() {}, resetView() {} };
    }

    const W = container.clientWidth || 480;
    const H = container.clientHeight || 360;
    const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    renderer.setPixelRatio(window.devicePixelRatio || 1);
    renderer.setSize(W, H);
    container.appendChild(renderer.domElement);

    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(45, W / H, 0.1, 10000);
    scene.add(new THREE.AmbientLight(0xffffff, 0.6));
    const key = new THREE.DirectionalLight(0xffffff, 0.85);
    key.position.set(1, 1.2, 1);
    scene.add(key);
    const fill = new THREE.DirectionalLight(0xffffff, 0.35);
    fill.position.set(-1, -0.4, -0.8);
    scene.add(fill);

    geometry.computeVertexNormals();
    const material = new THREE.MeshPhongMaterial({
      color: 0x9fb0d8,
      specular: 0x333333,
      shininess: 24,
    });
    const mesh = new THREE.Mesh(geometry, material);
    // CAD STLs are Z-up; stand the part up so Y is up (Three.js convention).
    const group = new THREE.Group();
    group.rotation.x = -Math.PI / 2;
    group.add(mesh);
    scene.add(group);

    const controls = new THREE.OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.dampingFactor = 0.08;
    controls.screenSpacePanning = true; // right-drag pans in the view plane
    frame(camera, controls, group);

    let raf = 0;
    function animate() {
      raf = requestAnimationFrame(animate);
      controls.update();
      renderer.render(scene, camera);
    }
    animate();

    const ro = new ResizeObserver(() => {
      const w = container.clientWidth,
        h = container.clientHeight;
      if (!w || !h) return;
      camera.aspect = w / h;
      camera.updateProjectionMatrix();
      renderer.setSize(w, h);
    });
    ro.observe(container);

    if (opts.onReady) opts.onReady();

    return {
      dispose() {
        cancelAnimationFrame(raf);
        ro.disconnect();
        controls.dispose();
        geometry.dispose();
        material.dispose();
        renderer.dispose();
        if (renderer.domElement.parentNode) {
          renderer.domElement.parentNode.removeChild(renderer.domElement);
        }
      },
      resetView() {
        frame(camera, controls, group);
      },
    };
  }

  return { create };
})();

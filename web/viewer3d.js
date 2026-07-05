// Interactive 3D STL viewer. Uses the vendored Three.js (global THREE) +
// STLLoader + OrbitControls — no build step, works offline. Orbit (drag), zoom
// (wheel), pan (right-drag).
//
// Vulcan3D.create(container, stlUrl, { token, fallbackImg }) -> handle
//   single mesh (a one-piece design).
// Vulcan3D.createAssembly(container, parts, { token, fallbackImg }) -> handle
//   parts = [{ url, colorIndex, name }] — each piece a distinct colour, and on
//   load they animate from an exploded layout INTO their assembled positions.
//   handle.dispose() / handle.resetView() / handle.replay()
window.Vulcan3D = (function () {
  // Distinct part colours, roughly matching api/rendering.PART_PALETTE so the PNG
  // preview and the 3D viewer agree on which piece is which.
  const PART_COLORS = [
    0xfa6b1c, 0x4a9ef0, 0x6bcc5c, 0xf2c73f, 0xb873eb, 0x40ccc7, 0xf272b3, 0x99a6b8,
  ];

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

  // Shared WebGL scene: renderer + camera + lights + a Z-up→Y-up group + orbit
  // controls + a render loop that also calls each registered per-frame hook.
  function initScene(container) {
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

    const group = new THREE.Group();
    group.rotation.x = -Math.PI / 2; // CAD is Z-up; stand it up so Y is up
    scene.add(group);

    const controls = new THREE.OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.dampingFactor = 0.08;
    controls.screenSpacePanning = true;

    const hooks = [];
    let raf = 0;
    function animate() {
      raf = requestAnimationFrame(animate);
      for (const h of hooks) h();
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

    return {
      renderer,
      scene,
      camera,
      controls,
      group,
      hooks,
      teardown(disposables) {
        cancelAnimationFrame(raf);
        ro.disconnect();
        controls.dispose();
        for (const d of disposables || []) {
          try {
            d.dispose();
          } catch (e) {
            /* ignore */
          }
        }
        renderer.dispose();
        if (renderer.domElement.parentNode) {
          renderer.domElement.parentNode.removeChild(renderer.domElement);
        }
      },
    };
  }

  async function fetchGeometry(url, token) {
    const headers = token ? { "X-Review-Token": token } : {};
    const resp = await fetch(url, { headers });
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    const geom = new THREE.STLLoader().parse(await resp.arrayBuffer());
    geom.computeVertexNormals();
    return geom;
  }

  async function create(container, stlUrl, opts = {}) {
    container.innerHTML = "";
    if (typeof THREE === "undefined" || !THREE.STLLoader || !THREE.OrbitControls) {
      showFallback(container, opts, "3D library failed to load.");
      return { dispose() {}, resetView() {}, replay() {} };
    }
    let geometry;
    try {
      geometry = await fetchGeometry(stlUrl, opts.token);
    } catch (err) {
      showFallback(container, opts, `3D preview unavailable (${err.message}).`);
      return { dispose() {}, resetView() {}, replay() {} };
    }

    const s = initScene(container);
    const material = new THREE.MeshPhongMaterial({
      color: 0x9fb0d8,
      specular: 0x333333,
      shininess: 24,
    });
    s.group.add(new THREE.Mesh(geometry, material));
    frame(s.camera, s.controls, s.group);
    if (opts.onReady) opts.onReady();

    return {
      dispose() {
        s.teardown([geometry, material]);
      },
      resetView() {
        frame(s.camera, s.controls, s.group);
      },
      replay() {},
    };
  }

  async function createAssembly(container, parts, opts = {}) {
    container.innerHTML = "";
    if (typeof THREE === "undefined" || !THREE.STLLoader || !THREE.OrbitControls) {
      showFallback(container, opts, "3D library failed to load.");
      return { dispose() {}, resetView() {}, replay() {} };
    }

    // Load every part; skip any that fail (e.g. one gated file).
    const loaded = [];
    for (let i = 0; i < parts.length; i++) {
      try {
        const geom = await fetchGeometry(parts[i].url, opts.token);
        loaded.push({ geom, colorIndex: parts[i].colorIndex != null ? parts[i].colorIndex : i });
      } catch (e) {
        /* skip this part */
      }
    }
    if (!loaded.length) {
      showFallback(container, opts, "3D preview unavailable.");
      return { dispose() {}, resetView() {}, replay() {} };
    }

    const s = initScene(container);
    const disposables = [];

    // Assembly centre (in group-local coords) so we can push each part outward
    // along its direction from the centre for the "exploded" start state.
    const asmBox = new THREE.Box3();
    const partData = [];
    for (const item of loaded) {
      const mat = new THREE.MeshPhongMaterial({
        color: PART_COLORS[item.colorIndex % PART_COLORS.length],
        specular: 0x222222,
        shininess: 26,
      });
      const mesh = new THREE.Mesh(item.geom, mat);
      const holder = new THREE.Group(); // moved during the explode animation
      holder.add(mesh);
      s.group.add(holder);
      disposables.push(item.geom, mat);

      const box = new THREE.Box3().setFromObject(mesh);
      const c = box.getCenter(new THREE.Vector3());
      partData.push({ holder, center: c });
      asmBox.union(box);
    }

    const asmCenter = asmBox.getCenter(new THREE.Vector3());
    const asmSize = asmBox.getSize(new THREE.Vector3());
    const spread = (Math.max(asmSize.x, asmSize.y, asmSize.z) || 1) * 0.9;
    for (const pd of partData) {
      const dir = pd.center.clone().sub(asmCenter);
      if (dir.lengthSq() < 1e-6) dir.set(0, 0, 1); // a centred part pops "up"
      pd.dir = dir.normalize().multiplyScalar(spread);
    }

    // Frame to the EXPLODED extent so nothing leaves the view mid-animation.
    for (const pd of partData) pd.holder.position.copy(pd.dir);
    frame(s.camera, s.controls, s.group);

    // Explode animation: t goes 1 (apart) -> 0 (assembled) with an ease-out.
    const DURATION = 1500;
    let start = performance.now();
    const easeOut = (x) => 1 - Math.pow(1 - x, 3);
    s.hooks.push(() => {
      const p = Math.min(1, (performance.now() - start) / DURATION);
      const t = 1 - easeOut(p); // 1 -> 0
      for (const pd of partData) {
        pd.holder.position.copy(pd.dir).multiplyScalar(t);
      }
    });

    if (opts.onReady) opts.onReady();

    return {
      dispose() {
        s.teardown(disposables);
      },
      resetView() {
        frame(s.camera, s.controls, s.group);
      },
      replay() {
        start = performance.now();
      },
    };
  }

  return { create, createAssembly };
})();

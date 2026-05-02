/* qr-reader.js — camera-based QR code decoder using jsQR */
(function () {
    "use strict";

    const video = document.getElementById("qr-video");
    const overlay = document.getElementById("qr-overlay");
    const placeholder = document.getElementById("qr-placeholder");
    const startBtn = document.getElementById("btn-start");
    const stopBtn = document.getElementById("btn-stop");
    const resultBox = document.getElementById("result-box");
    const resultText = document.getElementById("result-text");
    const copyBtn = document.getElementById("btn-copy");
    const errorMsg = document.getElementById("error-msg");
    const cameraSelect = document.getElementById("camera-select");
    const cameraWrap = document.getElementById("camera-select-wrap");
    const historyList = document.getElementById("history-list");
    const historySection = document.getElementById("result-history");

    let stream = null;
    let rafId = null;
    let canvas = null;
    let ctx = null;
    let lastCode = null;
    const history = [];

    // Build camera list
    async function buildCameraList() {
        try {
            const devices = await navigator.mediaDevices.enumerateDevices();
            const cams = devices.filter((d) => d.kind === "videoinput");
            cameraSelect.innerHTML = "";
            if (cams.length === 0) {
                const opt = document.createElement("option");
                opt.textContent = "カメラが見つかりません";
                cameraSelect.appendChild(opt);
                startBtn.disabled = true;
                return;
            }
            cams.forEach((cam, i) => {
                const opt = document.createElement("option");
                opt.value = cam.deviceId;
                opt.textContent = cam.label || `カメラ ${i + 1}`;
                // prefer back camera
                if (/back|rear|environment/i.test(cam.label)) {
                    opt.selected = true;
                }
                cameraSelect.appendChild(opt);
            });
            cameraWrap.hidden = cams.length <= 1;
        } catch (err) {
            // Permissions not granted yet — labels are unavailable until getUserMedia is called.
            // Hide the selector silently; it will be rebuilt after the first startCamera() call.
            console.debug("enumerateDevices before permission grant:", err);
            cameraWrap.hidden = true;
        }
    }

    async function startCamera() {
        stopCamera();
        showError(null);

        const deviceId = cameraSelect.value;
        const videoConstraints = deviceId
            ? { deviceId: { exact: deviceId } }
            : { facingMode: { ideal: "environment" } };

        try {
            stream = await navigator.mediaDevices.getUserMedia({ video: videoConstraints, audio: false });
        } catch (err) {
            let msg;
            if (err.name === "NotAllowedError" || err.name === "PermissionDeniedError") {
                msg = "カメラへのアクセスが拒否されました。ブラウザの設定でカメラを許可してください。";
            } else if (err.name === "NotFoundError") {
                msg = "カメラが見つかりません。デバイスにカメラが接続されているか確認してください。";
            } else {
                msg = `カメラを起動できませんでした: ${err.message}`;
            }
            showError(msg);
            return;
        }

        video.srcObject = stream;
        await video.play();

        placeholder.hidden = true;
        stopBtn.disabled = false;
        startBtn.disabled = true;
        cameraSelect.disabled = true;

        // Rebuild list now that we have permission (labels become available)
        await buildCameraList();
        if (deviceId) cameraSelect.value = deviceId;

        canvas = document.createElement("canvas");
        ctx = canvas.getContext("2d", { willReadFrequently: true });

        scanLoop();
    }

    function stopCamera() {
        if (rafId) {
            cancelAnimationFrame(rafId);
            rafId = null;
        }
        if (stream) {
            stream.getTracks().forEach((t) => t.stop());
            stream = null;
        }
        video.srcObject = null;
        clearOverlay();
        placeholder.hidden = false;
        startBtn.disabled = false;
        stopBtn.disabled = true;
        cameraSelect.disabled = false;
        lastCode = null;
    }

    function scanLoop() {
        if (!stream) return;
        if (video.readyState === video.HAVE_ENOUGH_DATA) {
            canvas.width = video.videoWidth;
            canvas.height = video.videoHeight;
            ctx.drawImage(video, 0, 0);
            const imageData = ctx.getImageData(0, 0, canvas.width, canvas.height);
            const code = jsQR(imageData.data, imageData.width, imageData.height, {
                inversionAttempts: "dontInvert",
            });
            if (code) {
                drawOverlay(code.location, canvas.width, canvas.height);
                if (code.data !== lastCode) {
                    lastCode = code.data;
                    showResult(code.data);
                }
            } else {
                clearOverlay();
            }
        }
        rafId = requestAnimationFrame(scanLoop);
    }

    function drawOverlay(location, w, h) {
        const rect = video.getBoundingClientRect();
        const scaleX = rect.width / w;
        const scaleY = rect.height / h;

        const svgNS = "http://www.w3.org/2000/svg";
        overlay.innerHTML = "";

        const pts = [
            location.topLeftCorner,
            location.topRightCorner,
            location.bottomRightCorner,
            location.bottomLeftCorner,
        ];
        const points = pts.map((p) => `${p.x * scaleX},${p.y * scaleY}`).join(" ");

        const polygon = document.createElementNS(svgNS, "polygon");
        polygon.setAttribute("points", points);
        polygon.setAttribute("fill", "rgba(0,200,100,0.18)");
        polygon.setAttribute("stroke", "#00c864");
        polygon.setAttribute("stroke-width", "3");
        polygon.setAttribute("stroke-linejoin", "round");
        overlay.appendChild(polygon);
    }

    function clearOverlay() {
        overlay.innerHTML = "";
    }

    function showResult(text) {
        resultBox.hidden = false;
        const isUrl = /^https?:\/\//i.test(text);
        if (isUrl) {
            const a = document.createElement("a");
            a.href = text;
            a.textContent = text;
            a.target = "_blank";
            a.rel = "noopener noreferrer";
            resultText.innerHTML = "";
            resultText.appendChild(a);
        } else {
            resultText.textContent = text;
        }
        copyBtn.dataset.value = text;

        // History (avoid duplicates, keep max 10)
        if (!history.includes(text)) {
            history.unshift(text);
            if (history.length > 10) history.pop();
            renderHistory();
        }
    }

    function renderHistory() {
        if (history.length === 0) {
            historySection.hidden = true;
            return;
        }
        historySection.hidden = false;
        historyList.innerHTML = "";
        history.forEach((item) => {
            const li = document.createElement("li");
            const isUrl = /^https?:\/\//i.test(item);
            if (isUrl) {
                const a = document.createElement("a");
                a.href = item;
                a.textContent = item;
                a.target = "_blank";
                a.rel = "noopener noreferrer";
                li.appendChild(a);
            } else {
                li.textContent = item;
            }
            historyList.appendChild(li);
        });
    }

    function showError(msg) {
        if (msg) {
            errorMsg.textContent = msg;
            errorMsg.hidden = false;
        } else {
            errorMsg.hidden = true;
        }
    }

    // ── Event listeners ──────────────────────────────────────────────────────

    startBtn.addEventListener("click", startCamera);
    stopBtn.addEventListener("click", stopCamera);

    copyBtn.addEventListener("click", async () => {
        const text = copyBtn.dataset.value || "";
        if (!text) return;
        try {
            await navigator.clipboard.writeText(text);
            const orig = copyBtn.textContent;
            copyBtn.textContent = "コピー済 ✓";
            setTimeout(() => { copyBtn.textContent = orig; }, 1500);
        } catch (err) {
            // Clipboard API unavailable or denied. Show temporary feedback.
            console.warn("clipboard.writeText failed:", err);
            const orig = copyBtn.textContent;
            copyBtn.textContent = "コピー失敗";
            setTimeout(() => { copyBtn.textContent = orig; }, 1500);
        }
    });

    cameraSelect.addEventListener("change", () => {
        if (stream) startCamera();
    });

    // ── Init ─────────────────────────────────────────────────────────────────

    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        showError("このブラウザはカメラアクセス (MediaDevices API) に対応していません。最新のブラウザをお使いください。");
        startBtn.disabled = true;
    } else {
        buildCameraList();
    }
})();

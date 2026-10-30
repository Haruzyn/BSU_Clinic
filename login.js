const loginMessage = document.querySelector('#login-message');
const faceLoginButton = document.querySelector('#face-login');
const schoolIdLoginButton = document.querySelector('#school-id-login');
const emailLoginButton = document.querySelector('#email-login');
const loginOptions = document.querySelector('#login-options');
const emailForm = document.querySelector('#email-login-form');
const emailMessage = document.querySelector('#email-message');
const cameraPanel = document.querySelector('#face-camera');
const video = document.querySelector('#face-video');
const cameraMessage = document.querySelector('#face-message');
const captureButton = document.querySelector('#face-capture');
const scanInstructions = document.querySelector('#scan-instructions');
const manualIdForm = document.querySelector('#manual-id-form');
const manualIdInput = document.querySelector('#manual-id-number');
const manualIdSubmit = document.querySelector('#manual-id-submit');
const modelUrl = '/vendor/face-api/model';
let cameraStream = null;
let loginMode = 'face';
let modelsReady = false;
let ocrWorker = null;

async function postJson(url, body) {
    const response = await fetch(url, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Request failed.');
    return result;
}

function setMessage(element, message, isError = false) {
    element.textContent = message;
    element.classList.toggle('is-error', isError);
}

async function loadFaceModels() {
    if (modelsReady) return;
    if (!window.faceapi) throw new Error('The face recognition library did not load. Check your internet connection and refresh the page.');
    setMessage(cameraMessage, 'Loading face recognition models…');
    await Promise.all([
        faceapi.nets.tinyFaceDetector.loadFromUri(modelUrl),
        faceapi.nets.faceLandmark68Net.loadFromUri(modelUrl),
        faceapi.nets.faceRecognitionNet.loadFromUri(modelUrl)
    ]);
    modelsReady = true;
}

async function acquireCamera(forIdScan = false) {
    if (!navigator.mediaDevices?.getUserMedia) {
        throw new Error('Camera access is unavailable. Open this site on HTTPS or localhost and use a supported browser.');
    }
    const stream = await navigator.mediaDevices.getUserMedia({
        video: {
            facingMode: { ideal: forIdScan ? 'environment' : 'user' },
            width: { ideal: forIdScan ? 1920 : 640 },
            height: { ideal: forIdScan ? 1080 : 480 }
        },
        audio: false
    });
    return stream;
}

function stopCamera() {
    if (cameraStream) cameraStream.getTracks().forEach(track => track.stop());
    cameraStream = null;
    video.srcObject = null;
}

async function openCamera(mode) {
    loginMode = mode;
    loginOptions.hidden = true;
    cameraPanel.hidden = false;
    captureButton.disabled = true;
    manualIdForm.hidden = mode !== 'school-id';
    scanInstructions.textContent = mode === 'school-id'
        ? 'Hold your Student ID steady in front of the camera with the ID number clearly visible, then select Scan Student ID.'
        : 'Position your face in the camera and select Scan Face ID.';
    captureButton.textContent = mode === 'school-id' ? 'Scan Student ID' : 'Scan Face ID';
    try {
        if (mode !== 'school-id') await loadFaceModels();
        cameraStream = await acquireCamera(mode === 'school-id');
        video.srcObject = cameraStream;
        await video.play();
        setMessage(cameraMessage, mode === 'school-id'
            ? 'Camera ready. Keep the ID number in frame.'
            : 'Camera ready. Look at the camera.');
        captureButton.disabled = false;
    } catch (error) {
        setMessage(cameraMessage, error.message || 'Could not open the camera.', true);
        stopCamera();
    }
}

faceLoginButton.addEventListener('click', () => openCamera('face'));
schoolIdLoginButton.addEventListener('click', () => openCamera('school-id'));
emailLoginButton.addEventListener('click', () => {
    loginOptions.hidden = true;
    emailForm.hidden = false;
});

document.querySelector('#email-cancel').addEventListener('click', () => {
    emailForm.hidden = true;
    loginOptions.hidden = false;
});

emailForm.addEventListener('submit', async event => {
    event.preventDefault();
    const submitButton = document.querySelector('#email-submit');
    submitButton.disabled = true;
    setMessage(emailMessage, 'Signing in…');
    try {
        await postJson('/api/auth/login', {
            email: document.querySelector('#login-email').value.trim(),
            password: document.querySelector('#login-password').value
        });
        window.location.assign('/');
    } catch (error) {
        setMessage(emailMessage, error.message || 'Email or password is incorrect.', true);
        submitButton.disabled = false;
    }
});

document.querySelector('#face-cancel').addEventListener('click', () => {
    stopCamera();
    cameraPanel.hidden = true;
    loginOptions.hidden = false;
});

manualIdForm.addEventListener('submit', async event => {
    event.preventDefault();
    const digits = manualIdInput.value.replace(/\D/g, '');
    if (!/^\d{7}$/.test(digits)) {
        setMessage(cameraMessage, 'Enter a valid 7-digit Student ID number.', true);
        return;
    }
    manualIdSubmit.disabled = true;
    setMessage(cameraMessage, 'Looking up your Student ID…');
    try {
        await postJson('/api/auth/school-id/login', { idNumber: `${digits.slice(0, 2)}-${digits.slice(2)}` });
        stopCamera();
        window.location.assign('/');
    } catch (error) {
        setMessage(cameraMessage, error.message || 'This Student ID could not sign in.', true);
        manualIdSubmit.disabled = false;
    }
});

captureButton.addEventListener('click', async () => {
    captureButton.disabled = true;
    try {
        setMessage(cameraMessage, loginMode === 'school-id' ? 'Reading the Student ID number…' : 'Checking your face…');
        const canvas = document.createElement('canvas');
        canvas.width = video.videoWidth;
        canvas.height = video.videoHeight;
        if (!canvas.width || !canvas.height) throw new Error('Camera is not ready. Try again.');
        const context = canvas.getContext('2d');
        context.drawImage(video, 0, 0, canvas.width, canvas.height);
        let idNumber = null;
        if (loginMode === 'school-id') {
            if (!window.Tesseract) throw new Error('ID text recognition did not load. Refresh and try again.');
            if (!ocrWorker) ocrWorker = await Tesseract.createWorker('eng');
            const { data } = await ocrWorker.recognize(canvas);
            const idMatch = data.text.toUpperCase().replace(/[OQ]/g, '0').replace(/[IL]/g, '1')
                .match(/\b(\d{2})[\s-]*(\d{5})\b/);
            if (!idMatch) throw new Error('Could not read the Student ID number. Try the manual entry below or scan again.');
            idNumber = `${idMatch[1]}-${idMatch[2]}`;
        }
        if (loginMode === 'school-id') {
            await postJson('/api/auth/school-id/login', { idNumber });
        } else {
            const faces = await faceapi.detectAllFaces(canvas, new faceapi.TinyFaceDetectorOptions())
                .withFaceLandmarks().withFaceDescriptors();
            if (faces.length !== 1) {
                throw new Error(faces.length ? 'Only one clear face should be in frame.' : 'No face detected. Improve the lighting and look at the camera.');
            }
            await postJson('/api/auth/face/login', { descriptor: Array.from(faces[0].descriptor) });
        }
        stopCamera();
        window.location.assign('/');
    } catch (error) {
        setMessage(cameraMessage, error.message || 'Could not recognize the face. Please try again.', true);
        captureButton.disabled = false;
    }
});

window.addEventListener('pagehide', () => {
    stopCamera();
    if (ocrWorker) ocrWorker.terminate();
});

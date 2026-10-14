const loginForm = document.querySelector('#login-form');
const loginMessage = document.querySelector('#login-message');
const loginButton = loginForm.querySelector('button[type="submit"]');
const faceLoginButton = document.querySelector('#face-login');
const enrollmentPanel = document.querySelector('#passkey-setup');
const enrollButton = document.querySelector('#passkey-enroll');
const cameraPanel = document.querySelector('#face-camera');
const video = document.querySelector('#face-video');
const cameraMessage = document.querySelector('#face-message');
const captureButton = document.querySelector('#face-capture');
const modelUrl = '/vendor/face-api/model';
let cameraStream = null;
let faceMode = 'login';
let modelsReady = false;

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

async function openCamera(mode) {
	faceMode = mode;
	loginForm.hidden = mode === 'enroll';
	enrollmentPanel.hidden = true;
	cameraPanel.hidden = false;
	captureButton.disabled = true;
	try {
		await loadFaceModels();
		cameraStream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: 'user' }, audio: false });
		video.srcObject = cameraStream;
		await video.play();
		setMessage(cameraMessage, 'Camera ready. Look at the camera and select Scan face.');
		captureButton.disabled = false;
	} catch (error) {
		setMessage(cameraMessage, error.name === 'NotAllowedError'
			? 'Allow camera access in your browser settings, then try again.'
			: (error.message || 'Could not open the camera.'), true);
		stopCamera();
	}
}

function stopCamera() {
	if (cameraStream) cameraStream.getTracks().forEach(track => track.stop());
	cameraStream = null;
	video.srcObject = null;
}

loginForm.addEventListener('submit', async (event) => {
	event.preventDefault();
	if (!loginForm.reportValidity()) return;
	loginButton.disabled = true;
	setMessage(loginMessage, 'Signing in…');
	try {
		await postJson('/api/auth/login', {
			email: loginForm.elements.email.value.trim(),
			password: loginForm.elements.password.value
		});
		const statusResponse = await fetch('/api/auth/face/status');
		const status = await statusResponse.json();
		if (!status.registered) {
			loginForm.hidden = true;
			enrollmentPanel.hidden = false;
			setMessage(document.querySelector('#passkey-setup-message'), 'Enroll your face with the camera to enable face sign-in.');
		} else {
			window.location.assign('/');
		}
	} catch (error) {
		setMessage(loginMessage, error.message, true);
		loginButton.disabled = false;
	}
});

faceLoginButton.addEventListener('click', () => {
	if (!loginForm.elements.email.value.trim()) {
		setMessage(loginMessage, 'Enter your admin email first.', true);
		loginForm.elements.email.focus();
		return;
	}
	openCamera('login');
});

enrollButton.addEventListener('click', () => openCamera('enroll'));

captureButton.addEventListener('click', async () => {
	captureButton.disabled = true;
	try {
		setMessage(cameraMessage, 'Checking your face…');
		const faces = await faceapi.detectAllFaces(video, new faceapi.TinyFaceDetectorOptions())
			.withFaceLandmarks().withFaceDescriptors();
		if (faces.length !== 1) throw new Error(faces.length ? 'Only one face should be visible.' : 'No face detected. Improve the lighting and look at the camera.');
		const descriptor = Array.from(faces[0].descriptor);
		if (faceMode === 'enroll') {
			await postJson('/api/auth/face/enroll', {
				password: loginForm.elements.password.value,
				descriptor
			});
			stopCamera();
			window.location.assign('/');
		} else {
			await postJson('/api/auth/face/login', {
				email: loginForm.elements.email.value.trim(),
				descriptor
			});
			stopCamera();
			window.location.assign('/');
		}
	} catch (error) {
		setMessage(cameraMessage, error.message || 'Face not recognized. Please try again.', true);
		captureButton.disabled = false;
	}
});

document.querySelector('#face-cancel').addEventListener('click', () => {
	stopCamera();
	cameraPanel.hidden = true;
	if (faceMode === 'enroll') enrollmentPanel.hidden = false;
	else loginForm.hidden = false;
});

document.querySelector('#continue-login').addEventListener('click', () => window.location.assign('/'));
window.addEventListener('pagehide', stopCamera);

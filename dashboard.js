const allowedStatuses = new Set(['New', 'In progress', 'Resolved']);
const dateTimeFormatter = new Intl.DateTimeFormat(undefined, {
	month: 'short',
	day: 'numeric',
	hour: 'numeric',
	minute: '2-digit'
});
const dateFormatter = new Intl.DateTimeFormat(undefined, {
	weekday: 'long',
	month: 'long',
	day: 'numeric',
	year: 'numeric'
});

let alerts = [];
let devices = [];
let selectedAlertId = null;
let demoMode = false;

const alertsBody = document.querySelector('#alerts-body');
const devicesBody = document.querySelector('#devices-body');
const searchInput = document.querySelector('#alert-search');
const statusFilter = document.querySelector('#status-filter');
const statusForm = document.querySelector('#status-form');
const statusSelect = document.querySelector('#status-update');
const statusMessage = document.querySelector('#status-message');
const statusButton = statusForm.querySelector('button');
const apiConnection = document.querySelector('#api-connection');
const guardContactForm = document.querySelector('#guard-contact-form');
const guardPhoneInput = document.querySelector('#guard-phone');
const guardPhoneMessage = document.querySelector('#guard-phone-message');
const guardPhoneButton = document.querySelector('#guard-phone-save');

function escapeHtml(value) {
	return String(value).replace(/[&<>"']/g, (character) => ({
		'&': '&amp;',
		'<': '&lt;',
		'>': '&gt;',
		'"': '&quot;',
		"'": '&#39;'
	})[character]);
}

function statusClass(status) {
	if (status === 'In progress') return 'status-in-progress';
	if (status === 'Resolved') return 'status-resolved';
	return 'status-new';
}

function smsStatusClass(status) {
	if (['Sent', 'Delivered'].includes(status)) return 'sms-sent';
	if (status === 'Failed') return 'sms-failed';
	return 'sms-pending';
}

function formatPhilippineMobileNumber(phoneNumber) {
	const localNumber = phoneNumber.replace(/^\+63/, '0');
	if (/^09[0-9]{9}$/.test(localNumber)) {
		return `${localNumber.slice(0, 4)} ${localNumber.slice(4, 7)} ${localNumber.slice(7)}`;
	}
	return phoneNumber;
}

function createExampleAlerts() {
	const now = Date.now();
	return [
		['ALT-EXAMPLE-001', 'Rescue needed', 'RSQ-MB-001', 'Main Building', '2nd Floor', 2, 'New'],
		['ALT-EXAMPLE-002', 'First aid request', 'RSQ-SC-003', 'Science Building', '1st Floor', 18, 'In progress'],
		['ALT-EXAMPLE-003', 'Medical assistance', 'RSQ-GY-002', 'Gymnasium', 'Ground Floor', 35, 'Resolved'],
		['ALT-EXAMPLE-004', 'Rescue needed', 'RSQ-AD-001', 'Admin Building', 'Ground Floor', 52, 'New'],
		['ALT-EXAMPLE-005', 'First aid request', 'RSQ-LB-004', 'Library', '3rd Floor', 75, 'Resolved']
	].map(([id, type, deviceId, building, floor, minutesAgo, status]) => ({
		id,
		type,
		deviceId,
		building,
		floor,
		receivedAt: new Date(now - minutesAgo * 60_000).toISOString(),
		status,
		smsStatus: 'Demo only',
		isSample: true
	}));
}

async function apiRequest(url, options) {
	const response = await fetch(url, options);
	if (response.status === 401) {
		window.location.assign('/login.html');
		throw new Error('Your session has expired. Please sign in again.');
	}
	let payload;
	try {
		payload = await response.json();
	} catch {
		throw new Error('The server returned an invalid response.');
	}
	if (!response.ok) {
		throw new Error(payload.error || `The request failed (${response.status}).`);
	}
	return payload;
}

function setApiConnection(connected) {
	apiConnection.classList.toggle('is-error', !connected);
	apiConnection.innerHTML = `<span aria-hidden="true"></span>${connected ? 'API connected' : 'API unavailable'}`;
}

function renderAlerts() {
	const query = searchInput.value.trim().toLowerCase();
	const selectedStatus = statusFilter.value;
	const filteredAlerts = alerts.filter((alert) => {
		const matchesQuery = [
			alert.id,
			alert.type,
			alert.deviceId,
			alert.building,
			alert.floor
		].some((value) => String(value).toLowerCase().includes(query));
		return matchesQuery && (selectedStatus === 'all' || alert.status === selectedStatus);
	});

	alertsBody.innerHTML = filteredAlerts.map((alert) => `
		<tr data-alert-id="${escapeHtml(alert.id)}" tabindex="0" aria-label="View ${escapeHtml(alert.id)} details" class="${alert.id === selectedAlertId ? 'is-selected' : ''}">
			<td class="alert-id">${escapeHtml(alert.id)}${alert.isSample ? '<small class="sample-label">EXAMPLE</small>' : ''}</td>
			<td class="alert-type">${escapeHtml(alert.type)}</td>
			<td class="location-cell">${escapeHtml(alert.building)}<small>${escapeHtml(alert.floor)} · ${escapeHtml(alert.deviceId)}</small></td>
			<td>${escapeHtml(dateTimeFormatter.format(new Date(alert.receivedAt)))}</td>
			<td><span class="status-badge ${statusClass(alert.status)}">${escapeHtml(alert.status)}</span></td>
			<td><span class="sms-badge ${smsStatusClass(alert.smsStatus)}">${escapeHtml(alert.smsStatus)}</span></td>
		</tr>
	`).join('');

	document.querySelector('#empty-alerts').hidden = filteredAlerts.length > 0;
}

function renderDetails() {
	const alert = alerts.find((item) => item.id === selectedAlertId);
	const detailStatus = document.querySelector('#detail-status');

	if (!alert) {
		document.querySelector('#detail-type').textContent = 'Select an alert';
		document.querySelector('#detail-id').textContent = 'Choose an alert from the list to view its information.';
		document.querySelector('#detail-building').textContent = '—';
		document.querySelector('#detail-floor').textContent = '—';
		document.querySelector('#detail-device').textContent = '—';
		document.querySelector('#detail-time').textContent = '—';
		document.querySelector('#detail-sms').textContent = '—';
		detailStatus.textContent = 'No alert selected';
		detailStatus.className = 'status-badge';
		statusSelect.value = 'New';
		statusSelect.disabled = true;
		statusButton.disabled = true;
		return;
	}

	document.querySelector('#detail-type').textContent = alert.type;
	document.querySelector('#detail-id').textContent = alert.id;
	document.querySelector('#detail-building').textContent = alert.building;
	document.querySelector('#detail-floor').textContent = alert.floor;
	document.querySelector('#detail-device').textContent = alert.deviceId;
	document.querySelector('#detail-time').textContent = dateTimeFormatter.format(new Date(alert.receivedAt));
	document.querySelector('#detail-sms').textContent = alert.smsStatus;
	detailStatus.textContent = alert.status;
	detailStatus.className = `status-badge ${statusClass(alert.status)}`;
	statusSelect.value = alert.status;
	statusSelect.disabled = demoMode;
	statusButton.disabled = demoMode;
}

function renderDevices() {
	devicesBody.innerHTML = devices.map((device) => `
		<tr>
			<td class="device-id">${escapeHtml(device.id)}</td>
			<td>${escapeHtml(device.name)}</td>
			<td>${escapeHtml(device.building)}</td>
			<td>${escapeHtml(device.floor)}</td>
			<td><span class="connection ${device.online ? '' : 'offline'}">${device.online ? 'Online' : 'Offline'}</span></td>
		</tr>
	`).join('');
	document.querySelector('#total-devices').textContent = String(devices.length);
	document.querySelector('#device-count').textContent = `${devices.length} devices`;
}

function renderSummary() {
	document.querySelector('#total-alerts').textContent = String(alerts.length);
	document.querySelector('#open-alerts').textContent = String(
		alerts.filter((alert) => alert.status !== 'Resolved').length
	);
}

function selectAlert(alertId) {
	if (!alerts.some((alert) => alert.id === alertId)) return;
	selectedAlertId = alertId;
	renderAlerts();
	renderDetails();
	statusMessage.textContent = demoMode
		? 'Example alert only. Connect to the Flask server to manage live alerts.'
		: 'Status changes are saved to the SQLite database.';
	statusMessage.classList.remove('is-success');
}

function showLoadError(error) {
	demoMode = true;
	alerts = createExampleAlerts();
	devices = [];
	selectedAlertId = alerts[0].id;
	renderAlerts();
	renderDetails();
	renderSummary();
	renderDevices();
	document.querySelector('#empty-alerts').hidden = true;
	document.querySelector('#device-count').textContent = 'API unavailable';
	guardPhoneMessage.textContent = 'Connect to the Flask server to load or save the guard number.';
	statusMessage.textContent = `Showing example alerts only. API unavailable: ${error.message}`;
	statusMessage.classList.remove('is-success');
}

alertsBody.addEventListener('click', (event) => {
	const row = event.target.closest('tr[data-alert-id]');
	if (row) selectAlert(row.dataset.alertId);
});

alertsBody.addEventListener('keydown', (event) => {
	const row = event.target.closest('tr[data-alert-id]');
	if (row && (event.key === 'Enter' || event.key === ' ')) {
		event.preventDefault();
		selectAlert(row.dataset.alertId);
	}
});

searchInput.addEventListener('input', renderAlerts);
statusFilter.addEventListener('change', renderAlerts);

guardContactForm.addEventListener('submit', async (event) => {
	event.preventDefault();
	if (!guardContactForm.reportValidity()) return;

	guardPhoneButton.disabled = true;
	guardPhoneMessage.textContent = 'Saving guard number…';
	guardPhoneMessage.classList.remove('is-success');
	try {
		const result = await apiRequest('/api/admin/guard-contact', {
			method: 'PUT',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ phoneNumber: guardPhoneInput.value.trim() })
		});
		guardPhoneInput.value = formatPhilippineMobileNumber(result.phoneNumber);
		guardPhoneMessage.textContent = result.phoneNumber
			? 'Guard number saved. New station alerts will be sent to this number.'
			: 'Guard number removed. New station alerts will not send an SMS.';
		guardPhoneMessage.classList.add('is-success');
	} catch (error) {
		guardPhoneMessage.textContent = `Could not save guard number: ${error.message}`;
		guardPhoneMessage.classList.remove('is-success');
	} finally {
		guardPhoneButton.disabled = false;
	}
});

statusForm.addEventListener('submit', async (event) => {
	event.preventDefault();
	if (!selectedAlertId || !allowedStatuses.has(statusSelect.value)) {
		statusMessage.textContent = 'Select an alert and a valid status before saving.';
		statusMessage.classList.remove('is-success');
		return;
	}

	statusButton.disabled = true;
	statusSelect.disabled = true;
	statusMessage.textContent = 'Saving status…';
	statusMessage.classList.remove('is-success');

	try {
		const updatedAlert = await apiRequest(
			`/api/alerts/${encodeURIComponent(selectedAlertId)}`,
			{
				method: 'PATCH',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ status: statusSelect.value })
			}
		);
		alerts = alerts.map((alert) => alert.id === updatedAlert.id ? updatedAlert : alert);
		renderAlerts();
		renderDetails();
		renderSummary();
		statusMessage.textContent = `Status saved as ${updatedAlert.status}.`;
		statusMessage.classList.add('is-success');
	} catch (error) {
		statusSelect.disabled = false;
		statusButton.disabled = false;
		statusMessage.textContent = `Could not save status: ${error.message}`;
		statusMessage.classList.remove('is-success');
	}
});

async function loadDashboard() {
	statusMessage.textContent = 'Loading dashboard data from the API…';
	try {
		const [alertsResponse, devicesResponse, guardContact] = await Promise.all([
			apiRequest('/api/alerts'),
			apiRequest('/api/devices'),
			apiRequest('/api/admin/guard-contact')
		]);
		demoMode = false;
		alerts = alertsResponse;
		devices = devicesResponse;
		guardPhoneInput.value = formatPhilippineMobileNumber(guardContact.phoneNumber);
		selectedAlertId = alerts[0]?.id ?? null;
		renderAlerts();
		renderDetails();
		renderDevices();
		renderSummary();
		setApiConnection(true);
		statusMessage.textContent = 'Status changes are saved to the SQLite database.';
		guardPhoneMessage.textContent = guardContact.phoneNumber
			? 'Saved guard contact is ready for new alerts.'
			: 'No guard number is saved yet.';
	} catch (error) {
		setApiConnection(false);
		showLoadError(error);
	}
}

document.querySelector('#current-date').textContent = dateFormatter.format(new Date());
loadDashboard();

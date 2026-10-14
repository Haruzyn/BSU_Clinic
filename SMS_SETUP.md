# Guard rescue SMS setup

The dashboard saves one guard phone number in the SQLite database. When a RESQNET station posts a new alert, Flask records the alert and sends the guard an SMS containing the station's building and floor.

1. Create a Twilio account and get an Account SID, Auth Token, and SMS-capable Twilio phone number.
2. Copy `.env.example` to `.env` if you have not already done so.
3. Set `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, and `TWILIO_FROM_NUMBER` in `.env`. Keep the Auth Token private.
4. Replace the example values for `SECRET_KEY` and `RESQNET_DEVICE_API_KEY` with long, random secrets. Keep both private.
5. Restart Flask, sign in to the dashboard with your registered admin account, and save the guard's Philippine mobile number as `09171234567` or `+639171234567`. The app stores it in Twilio's `+63` format.
6. Configure each station to send an HTTP `POST` with JSON `{}` and header `X-Device-Key: <RESQNET_DEVICE_API_KEY>` to `/api/devices/<device_id>/alerts` when its RESQNET button is pressed. For example: `/api/devices/RSQ-MB-001/alerts`.

The server records every station alert even if SMS delivery fails. It returns `201` when Twilio accepts the message, `502` when sending fails, or `503` when no guard number is configured. The dashboard shows Twilio's initial submission status; delivery receipt updates are not configured.

The guard number and clinic dashboard require signing in. Admin registration is currently open to anyone who can verify an email address, so add an approval/invitation process before deploying this for real clinic use. Use HTTPS and set `SESSION_COOKIE_SECURE=true` in `.env` in production; keep the device API key private.

# Gmail verification email setup

The registration flow sends a short-lived verification code through Gmail SMTP. It will return an error until sender credentials are configured. `GMAIL_ADDRESS` is the site's sender account; registrants enter their own recipient address on the registration page.

1. Enable 2-Step Verification on the Gmail account that will send RESQNET email.
2. Create a Google App Password for this project. Do not use your normal Gmail password.
3. Copy `.env.example` to `.env` in the project folder if `.env` does not already exist.
4. Set `GMAIL_ADDRESS` to the sender Gmail address and `GMAIL_APP_PASSWORD` to an App Password generated for that same account. Do not use a regular Gmail password.
5. Install the project requirements with `python -m pip install -r requirements.txt`.
6. Restart Flask with `python app.py`.

Keep `.env` private and do not commit or share it. The verification code expires after 10 minutes, has a five-attempt limit, and sending is rate-limited.

# HomeTiles Bridge v0.7.1b12 (beta)

Wrong codes from Alarmo, Total Connect and Elmax.

- Alarmo reports a wrong code with an event instead of an error, so Home Assistant answered with success and a panel showed neither "Wrong code" nor the lockout. For Alarmo alarm panels the Bridge now waits up to two seconds for Alarmo's verdict: a wrong code is reported as "Wrong code" and counts towards the lockout, without entering codes in the Bridge. Other refusals, such as open sensors, are reported as a failed command.
- Total Connect ("Usercode is invalid") and Elmax ("Invalid disarm code provided.") report wrong codes in words the Bridge did not recognise; they now count as wrong codes as well.
- Other alarm panels and locks are unchanged. Codes under "Codes for locks and alarm panels" keep working as in v0.7.1b11.

Install: in HACS enable "Show beta versions" for HomeTiles Bridge, update, restart Home Assistant and reload the page with Ctrl+Shift+R.

Validation: 485 Bridge tests.

**Full Changelog:** https://github.com/GalusPeres/HomeTiles-Bridge/compare/v0.7.1b11...v0.7.1b12

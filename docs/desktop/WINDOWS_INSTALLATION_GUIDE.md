# Installing Copilot on Windows

## What you need

- Windows 10 (64-bit, version 1809 or newer) or Windows 11
- **15 GB of free disk space**: Copilot's data takes about 4.4 GB, and updating it needs room for a
  second copy while the new one is prepared
- 8 GB of memory (RAM) recommended
- An internet connection: always for the first start, and from time to time afterwards

You don't need to install anything else. Copilot doesn't need Python, Node.js or any developer tools.

## Install

1. Run the Copilot setup file you were sent (its name ends in **-Setup.exe**).
2. If Windows shows **"Windows protected your PC"**, choose **More info**, then **Run anyway**.
   Windows shows this for new programs that aren't from a large publisher yet.
3. Setup takes under a minute. It doesn't ask for an administrator password. If your PC doesn't
   have the Microsoft Edge WebView2 component yet, setup adds it automatically.
4. Copilot opens by itself when setup finishes. Afterwards, open it from the **Start menu** or
   the **desktop shortcut**.

## The first start

- Copilot checks its licence over the internet. Your **2-day trial** starts the first time this
  succeeds.
- It then downloads the government rating list, business listings and Companies House data,
  about 4 GB in total. **This usually takes 1–3 hours**, depending on your connection. The download
  runs in the background, and a progress screen shows how far it has got.
- You can choose **Continue to Copilot** straight away. Searches work fully once all three datasets
  show **Installed**.
- If you close Copilot during the download, it asks first. The download starts again the next time
  you open Copilot.

## Updates

Copilot keeps itself up to date. When a new version is ready, a small notice appears in the corner:
choose **Restart now**, or **Later**, and it installs the next time you close Copilot. Your data,
sheets and settings are kept.

## Where Copilot keeps its files

| What | Where |
|---|---|
| The program | `C:\Users\<you>\AppData\Local\CopilotApp` |
| Data, sheets and settings | `C:\Users\<you>\AppData\Local\Copilot` |
| Log files (for support) | `C:\Users\<you>\AppData\Local\Copilot\logs` |

## Uninstall

Open **Settings → Apps → Installed apps**, find **Copilot**, and choose **Uninstall**.

Uninstalling removes the program but **keeps your data and sheets**, so reinstalling later
doesn't need the 4 GB download again. To free that space as well, delete the folder
`C:\Users\<you>\AppData\Local\Copilot` after uninstalling.

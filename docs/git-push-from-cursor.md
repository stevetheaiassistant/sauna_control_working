# Letting Cursor (AI) push to GitHub

When the assistant runs `git push` from Cursor, it often fails with:

```text
fatal: could not read Username for 'https://github.com': Device not configured
```

That happens because Git is using **HTTPS** and needs to prompt for credentials, but there’s no interactive terminal in the AI’s environment.

To allow push to work from Cursor (without you typing a password each time), use one of these.

---

## Option 1: Switch to SSH (recommended)

Git will use your SSH key instead of asking for a password.

**1. Use an SSH remote for this repo**

In Terminal (on your Mac), from the project folder:

```bash
cd /Users/wilson_design_ai/Documents/sauna_control
git remote set-url origin git@github.com:stevetheaiassistant/sauna_control.git
git remote -v
```

You should see `origin  git@github.com:stevetheaiassistant/sauna_control.git`.

**2. Make sure GitHub has your SSH key**

- If you’ve never set one up: [GitHub: Adding an SSH key](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/adding-a-new-ssh-key-to-your-github-account).
- Test: `ssh -T git@github.com` (should say you’re authenticated).

**3. Try push again**

From your Mac:

```bash
git push -u origin On-Scheduling
```

If that works, the assistant can run the same `git push` from Cursor and it should work too (Cursor uses your SSH agent when running commands).

---

## Option 2: Keep HTTPS and store a token

If you prefer HTTPS, use a **Personal Access Token (PAT)** and store it so Git doesn’t prompt.

**1. Create a PAT on GitHub**

- GitHub → **Settings** → **Developer settings** → **Personal access tokens** → **Tokens (classic)**.
- Generate a token with at least the **repo** scope.

**2. Store it so Git can use it**

In Terminal:

```bash
git config --global credential.helper osxkeychain
```

Then run **one** push from your Mac and when prompted:

- Username: your GitHub username  
- Password: paste the **PAT** (not your GitHub password)

macOS will store it in Keychain. After that, `git push` from your Mac (and often from Cursor) may work without prompting.

**3. If Cursor still can’t push**

Cursor’s environment might not see your keychain. In that case, **Option 1 (SSH)** is more reliable for “push from Cursor.”

---

## Summary

| Goal                         | Do this |
|-----------------------------|--------|
| Push works from your Mac    | Use SSH remote or one-time PAT + keychain. |
| Push also works from Cursor | Prefer **SSH remote** so the assistant uses your SSH key without a prompt. |

After switching to SSH (Option 1), tell the assistant to run `git push origin On-Scheduling` again; it should succeed if your SSH key is loaded in the agent.

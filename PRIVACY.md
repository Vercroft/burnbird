# Privacy

This page says what Burnbird stores about you, why, who else handles it, how long we keep it, and what you can do
about it. It covers the Burnbird website, burnbird.racingcrow.com, and the Burnbird GitHub App.

## Who is responsible

Vercroft runs Burnbird. Write to hello@burnbird.racingcrow.com with any question about your data. Messages sent there
are forwarded to Vercroft's company inbox.

## What we store, and what each item is for

When you sign in with GitHub:

| Item | Why we keep it | What it makes possible |
|---|---|---|
| Your GitHub user ID and login, and when you last signed in | To know who you are | Signing in |
| The installations of the App and the repositories you could see at your last sign-in, and whether you had admin on each | To show you only what you may see | The pages and their access rules |
| Your GitHub primary email address, if GitHub marks it verified, and when a sign-in last read it | To send you run-out warnings and service notices, which start in a later release | Email about your CI minutes |
| Your email choices: whether you want run-out warnings (on unless you turn them off), and two kinds not sent yet (a weekly summary and news, both off) | To honour your choice | Sending run-out warnings only while you want them |
| A record of each choice: which kind, on or off, where it came from (the default, or your own change), when, and which version of the wording you saw | To prove what you chose and when | Showing your choice if it is ever questioned |
| For each signed-in session: a SHA-256 of its random ID (never the ID itself), when it started and when it ends, and a short label of the device, such as "Firefox 140 on Linux, computer" | To keep you signed in, and so you can recognise your own sessions | Staying signed in |

For each GitHub account or organisation the App is installed on:

| Item | Why we keep it | What it makes possible |
|---|---|---|
| The account's ID, login and type, and the installation's ID and state | To know which account the data belongs to | The account's view |
| Each covered repository's ID, name and whether it is private | To group minutes by repository | The repository split |
| For each job attempt: its IDs, workflow name, runner labels, runner group and runner name, its created, started and completed times, its result, and the minutes and cost we work out from them | To count minutes | Minutes used and the run-out forecast |
| The settings you choose: the GitHub plan, the cycle start days and the sizes of larger runners | To count against the right allowance | The forecast |
| The fields we use from each webhook GitHub sends us (never the whole message) | To turn it into job rows once | Collecting jobs |

We never store your GitHub token, the logs, code, workflow files or secrets of any repository, or the raw header your
browser sends to describe itself.

## Why we may use it

Everything above is used to provide the service you signed up for: showing your CI minutes and when they run out.
Run-out warnings and service notices by email, when they start, are part of that service, and you will be able to
turn the warnings off. News about Burnbird will be sent only if you ask for it.

## Who else handles it

- **GitHub** is where your sign-in and every job's data come from.
- **Cloudflare** carries the website's traffic, and forwards messages sent to hello@burnbird.racingcrow.com to
  Vercroft's company inbox.
- No one else receives your email address today. When email starts, the address and each message will go to Zoho
  CPaaS, in India, to be sent, and this page will say so first.

## How long we keep it

| Item | Kept |
|---|---|
| Job attempts | 90 days after the job was created |
| Webhook fields | 7 days |
| A session | until it ends, 8 hours after sign-in, or you sign out |
| Your email address | 30 days after the last sign-in that read it, then deleted; a sign-in reads it again |
| Your user record and access lists | until you delete your data or revoke the App's authorization on GitHub |
| Your email choices and their records | until you delete your data or revoke the App's authorization on GitHub |
| An account's data | until the App is uninstalled from it |

Expired data is deleted once a day. Database backups keep deleted data until they are replaced by newer backups.

## Your rights, and how to use them

- **See what is stored:** this page lists it. Write to the contact point for a copy of what we hold about you.
- **Erase it:** "Delete my data" removes your user record, your email address and choices and their records, your
  sessions and your access lists, and the jobs of your personal account. Its page comes in a later release; until
  then, write to the contact point. Revoking the App's authorization on GitHub deletes the same, except the jobs, as
  soon as GitHub tells us. Uninstalling the App deletes everything stored for that account. An organisation's data
  belongs to the organisation, so it goes when the App is uninstalled from it.
- **Stop email:** none is sent yet. When it starts, each warning will carry a one-click unsubscribe link.
- **Ask a question:** write to hello@burnbird.racingcrow.com.
- **Complain:** you can complain to the data protection authority where you live; in India, the Data Protection
  Board of India.

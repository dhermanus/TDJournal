# Security Policy

## Supported Versions

TDJournal is an actively developed open-source project.

Security fixes are generally applied to the latest released version and the current `main` branch.

| Version               | Supported |
| --------------------- | --------- |
| Latest release        | ✅         |
| Current `main` branch | ✅         |
| Older releases        | ❌         |

## Reporting a Vulnerability

If you discover a security vulnerability in TDJournal, please **do not open a public GitHub issue**.

Instead, please use GitHub's **private vulnerability reporting** feature from the Security section of this repository.

Please include, when possible:

* A description of the vulnerability
* Steps to reproduce the issue
* The potential impact
* Any affected files or components
* Suggested fixes, if you have any

Please avoid publicly disclosing the vulnerability until it has been reviewed and, when necessary, a fix has been released.

## Security-Sensitive Areas

TDJournal works with potentially sensitive information including:

* Broker trade exports
* Trading history and performance data
* Locally stored journal data
* API keys for optional external services
* Uploaded diary files and images

Reports involving exposure of this information, unauthorized access, credential handling, unexpected network communication, or unsafe file handling are particularly important.

## Data and Credentials

TDJournal is designed as a local-first application.

Users should never commit API keys, `.env` files, local databases, broker exports, or other private trading data to the repository.

API credentials should only be stored using the configuration methods documented by the project.

## Responsible Disclosure

Please allow reasonable time to investigate and address a reported vulnerability before publicly discussing it.

Security researchers who responsibly report valid vulnerabilities are appreciated and may be credited in the corresponding release notes or security advisory if they wish.

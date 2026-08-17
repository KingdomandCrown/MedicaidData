# Documents

## MinervaAI_Client_Onboarding_Playbook.docx

The standard procedure for adding a hospital client to MinervaAI so it sees its
own data and its position against peers, and nothing else. Covers the commercial
checklist, provisioning, cohort governance, the isolation acceptance test,
ongoing access review, offboarding, and incident response, plus reusable forms
and client-facing language.

Regenerate after editing the source script:

```bash
npm install docx          # once
node docs/build-onboarding-playbook.js
```

Edit `build-onboarding-playbook.js` rather than the `.docx` when the change
should persist — the script is the source of truth, and a hand-edited document
is overwritten by the next build. Small wording fixes made directly in Word are
fine as long as they are folded back into the script.

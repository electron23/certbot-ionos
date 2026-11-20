certsync share
==============

certs/     Issued certificates as real files, one folder per certificate:
             certs/<name>/fullchain.pem, privkey.pem, cert.pem, chain.pem
           Written by certsync after every cycle. Treat as read-only,
           changes you make here are overwritten.
           privkey.pem is a secret.

scripts/   Your own deploy scripts (.sh, .py or executable files).
           Use them with a target of TYPE=script, see example-deploy.sh.

config/    certsync.env  - YOUR configuration: certificates (CERTS, CERT_*) and
                           deploy targets (TARGET_*). Create it by copying
                           certsync.env.example and editing it.
                           Re-read automatically, a change starts a new cycle
                           within ~30 seconds. No container restart needed.
                           Values here override container environment variables.
           certsync.env.example  - all options with comments.
           Everyone with access to the share can read these files, so put
           target passwords here only if that is acceptable.

Until certsync.env exists, the container waits and logs what is missing.

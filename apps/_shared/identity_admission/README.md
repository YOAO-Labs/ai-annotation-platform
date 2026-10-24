# Identity admission

`aap-identity-admission` contains the closed, language-neutral authentication
contract and the small HTTP client used by trusted API runtimes. It has no
database, ORM, FastAPI, or Identity-runtime dependency.

Install the `http` extra when using `RemoteAdmissionClient`. The client sends
only the admitted token to `/v1/identity/authenticate`; `actor_status` sends
only an actor UUID to `/v1/identity/actor-status`. Both calls use the configured
service bearer, ignore proxy environment variables, bound response bodies and
timeouts, and sanitize transport failures.

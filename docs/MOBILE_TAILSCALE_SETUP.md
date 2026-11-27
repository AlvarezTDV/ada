# Use ADA on Android over mobile data

Tailscale Serve terminates HTTPS inside your tailnet and proxies to ADA on
`127.0.0.1`. Use **Serve**, never **Funnel** or router port forwarding.

## Set up the PC and Android

1. Connect the PC and Android to the same tailnet. In the Tailscale Admin
   Console, open **DNS**, enable **MagicDNS**, then enable **HTTPS Certificates**.
   The PC's DNS name becomes public in certificate logs, not the service itself.
2. Set `ADA_TOKEN` privately as described in the [README](../README.md#local-http-api),
   then run `python -B -m ada_server.app` on the PC. Leave `ADA_TLS_CERTFILE` and
   `ADA_TLS_KEYFILE` unset: Serve provides HTTPS. Verify that
   `curl.exe -i http://127.0.0.1:8000/health` returns `200` and `{"status":"ok"}`.
3. With Tailscale v1.52 or later, run this on the PC:

   ```powershell
   tailscale serve --bg --https=443 --set-path=/ http://127.0.0.1:8000
   tailscale serve status
   ```

   Confirm `tailnet only`, the HTTPS `*.ts.net` hostname, and `/` proxying to
   `http://127.0.0.1:8000`. Use the printed URL (`https://<pc>.<tailnet>.ts.net`),
   not the PC's IP. The old positional `https /` syntax is not for current clients.
4. Open that URL in Chrome on Android. From the menu, choose **Add to Home
   screen** (**Agregar a pantalla de inicio**) and **Install** if prompted.
   Launch **A.D.A** from its icon. The PC and ADA must stay running.

## Smoke test — Wi-Fi off

- [ ] With Wi-Fi off, mobile data on, and Tailscale connected, launch A.D.A.
- [ ] In an incognito Chrome tab, open the same host with `/status` appended.
  Expect HTTP `401` with `{"detail":"Authentication required"}`. Also try an
  incorrect token in the sign-in form; ADA must reject it.
- [ ] Sign in with the correct token. Check **Conectado** and **/estado**.
- [ ] Create a temporary and a persistent session; switch between them and
  send a message. Confirm a readable response.
- [ ] Send `/navegar https://example.com Resume la página en una frase`.
  Confirm live progress, the final answer and an HTTPS result link. Start
  another navigation and tap **Cancelar** before it finishes.
- [ ] Tap **Cerrar sesión**. Reopen `/status` in that browser profile and
  confirm `401` again.

Reference: [Tailscale Serve CLI](https://tailscale.com/docs/reference/tailscale-cli/serve).

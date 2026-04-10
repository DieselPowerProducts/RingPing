# RingCentral Setup

## Recommended app type

Start with a `REST API App` in the RingCentral Developer Console. That is the best fit for reading Team Messaging events and posting updates back.

Use an add-in or bot later only if you want the workflow to live inside the RingCentral client itself.

## Keys and values you need

- `RINGPING_RINGCENTRAL_SERVER_URL`
  - Production is usually `https://platform.ringcentral.com`
  - Sandbox is usually `https://platform.devtest.ringcentral.com`
- `RINGPING_RINGCENTRAL_CLIENT_ID`
- `RINGPING_RINGCENTRAL_CLIENT_SECRET`
- `RINGPING_RINGCENTRAL_JWT`
  - Easiest for a private internal service.
  - If you do not want JWT, you can swap this later for auth-code flow.
- `RINGPING_RINGCENTRAL_VERIFICATION_TOKEN`
  - Used when creating webhook subscriptions.
- `RINGPING_RINGCENTRAL_VALIDATION_TOKEN`
  - Useful during webhook validation flows.
- `RINGPING_WEBHOOK_PUBLIC_BASE_URL`
  - RingCentral must reach your webhook over public HTTPS.
  - If this app stays on your desktop, you will need a tunnel or reverse proxy.

## Scopes and permissions to request

Based on the current RingCentral docs:

- `TeamMessaging`
  - Needed to view and post Team Messaging content.
- `SubscriptionWebhook`
  - Needed to create and manage webhook subscriptions.
- `UnifiedAppDesktop`
  - The Team Messaging post-event docs list this as required for `/team-messaging/v1/posts`.

RingCentral also notes that user permissions must allow the same actions, not just the app scope.

## Chat/thread info you need from RingCentral

For each project section in RingPing, you need the Team Messaging chat/group ID you want to treat as that project's request intake thread.

Store those IDs in `ringcentral_chat_ids` in `config/projects.json`.

## Public webhook requirement

This starter runs a local webhook server. RingCentral cannot call `http://127.0.0.1` from the internet.

You need one of:

- A small always-on VM or server running RingPing
- A reverse proxy in front of your desktop app
- A tunnel such as ngrok or Cloudflare Tunnel

The webhook path used by this starter is:

`/ringcentral/webhook`

## Suggested first production setup

1. Create the app in RingCentral.
2. Add the scopes above.
3. Generate a JWT credential.
4. Stand RingPing up on a machine that is always on.
5. Set the public webhook URL in `.env`.
6. Create a subscription for `/team-messaging/v1/posts`.
7. Put each project thread's `groupId` into `config/projects.json`.

## Source notes

- RingCentral Application permissions and scopes
- RingCentral Team Messaging post event filter
- RingCentral registering apps and JWT credentials

#!/usr/bin/env node
/**
 * CoWorker XMTP Bridge — local HTTP ↔ XMTP relay.
 *
 * Connects to the XMTP network using an Ethereum private key,
 * listens for incoming XMTP messages, and exposes a local HTTP API
 * so Python scripts can send/receive messages without Node.js deps.
 *
 * Compatible with @xmtp/node-sdk v5.x API.
 *
 * Env vars:
 *   XMTP_PRIVATE_KEY  — hex private key (from wallet.json)
 *   XMTP_ENV          — "dev" | "production" | "local" (default: "dev")
 *   BRIDGE_PORT       — local HTTP port (default: 3555)
 *
 * Local API (localhost only):
 *   POST /send               — send text message {to, content}
 *   POST /send-attachment     — send file/image {to, filename, mimeType, content(base64)}
 *   POST /send-remote-attachment — send large file ref {to, url, contentDigest, ...}
 *   POST /broadcast           — send to multiple recipients {to:[], content}
 *   GET  /inbox               — get buffered incoming messages [?since=ISO&clear=1]
 *   GET  /health              — bridge status
 *   POST /clear-inbox         — clear the inbox buffer
 *   GET  /can-message         — check if address can receive XMTP {?address=0x...}
 *   GET  /stream              — SSE stream of incoming messages (real-time push)
 */

import { Client, IdentifierKind } from "@xmtp/node-sdk";
import { privateKeyToAccount } from "viem/accounts";
import { toBytes } from "viem";
import express from "express";
import fs from "node:fs";
import path from "node:path";

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------
const PRIVATE_KEY = process.env.XMTP_PRIVATE_KEY;
const XMTP_ENV = process.env.XMTP_ENV || "dev";
const BRIDGE_PORT = parseInt(process.env.BRIDGE_PORT || "3500", 10);

if (!PRIVATE_KEY) {
  console.error("ERROR: XMTP_PRIVATE_KEY env var is required (hex private key from wallet.json)");
  process.exit(1);
}

// ---------------------------------------------------------------------------
// In-memory inbox buffer
// ---------------------------------------------------------------------------
const inbox = [];
const MAX_INBOX = 5000;
const sseClients = new Set();

// ---------------------------------------------------------------------------
// XMTP Client Setup (v5.x API)
// ---------------------------------------------------------------------------
const key = PRIVATE_KEY.startsWith("0x") ? PRIVATE_KEY : `0x${PRIVATE_KEY}`;
const account = privateKeyToAccount(key);
const walletAddress = account.address.toLowerCase();

console.log(`[XMTP] Wallet: ${walletAddress}`);
console.log(`[XMTP] Environment: ${XMTP_ENV}`);

const signer = {
  type: "EOA",
  getIdentifier: () => ({
    identifier: walletAddress,
    identifierKind: IdentifierKind.Ethereum,
  }),
  signMessage: async (message) => {
    const sig = await account.signMessage({ message });
    return toBytes(sig);
  },
};

// Persistent encryption key for local DB (survives restarts)
const dataDir = process.env.COWORKER_DATA_DIR || process.env.AGENTFAX_DATA_DIR || "";
const dbKeyPath = dataDir
  ? path.join(dataDir, ".xmtp_db_key")
  : path.join(process.env.HOME || "/tmp", ".xmtp_db_key");

let dbEncryptionKey;
if (fs.existsSync(dbKeyPath)) {
  const keyHex = fs.readFileSync(dbKeyPath, "utf8").trim();
  dbEncryptionKey = new Uint8Array(Buffer.from(keyHex, "hex"));
  console.log(`[XMTP] Loaded DB encryption key from ${dbKeyPath}`);
} else {
  dbEncryptionKey = crypto.getRandomValues(new Uint8Array(32));
  fs.writeFileSync(dbKeyPath, Buffer.from(dbEncryptionKey).toString("hex"), "utf8");
  console.log(`[XMTP] Generated new DB encryption key → ${dbKeyPath}`);
}

const xmtpClient = await Client.create(signer, {
  dbEncryptionKey,
  env: XMTP_ENV,
});

console.log(`[XMTP] Connected! Inbox ID: ${xmtpClient.inboxId}`);

// ---------------------------------------------------------------------------
// Helper: create identifier object for XMTP v5.x
// ---------------------------------------------------------------------------
function ethIdentifier(address) {
  return {
    identifier: address.toLowerCase(),
    identifierKind: IdentifierKind.Ethereum,
  };
}

// Helper: get or create DM conversation with a wallet address
async function getDm(targetAddress) {
  const canMessageResult = await xmtpClient.canMessage([ethIdentifier(targetAddress)]);
  const canSend = canMessageResult.get(targetAddress);
  if (!canSend) {
    throw new Error(`Address ${targetAddress} is not reachable on XMTP (${XMTP_ENV}).`);
  }
  await xmtpClient.conversations.sync();
  const dm = await xmtpClient.conversations.createDmWithIdentifier(
    ethIdentifier(targetAddress)
  );
  await dm.sync();
  return dm;
}

// Helper: get a group conversation by ID
const groupCache = new Map();

async function getGroup(groupId) {
  if (groupCache.has(groupId)) {
    const group = groupCache.get(groupId);
    await group.sync();
    return group;
  }
  await xmtpClient.conversations.sync();
  const conv = await xmtpClient.conversations.getConversationById(groupId);
  if (conv) {
    groupCache.set(groupId, conv);
    await conv.sync();
    return conv;
  }
  throw new Error(`Group ${groupId} not found`);
}

// ---------------------------------------------------------------------------
// Start stream BEFORE Express (critical ordering for XMTP native SDK)
// ---------------------------------------------------------------------------
await xmtpClient.conversations.sync();
const stream = await xmtpClient.conversations.streamAllMessages();
(async () => {
  for await (const message of stream) {
    try {
      if (message.senderInboxId === xmtpClient.inboxId) continue;

      const contentTypeId = message.contentType?.typeId || "text";
      let content = message.content;
      let attachment = null;

      if (contentTypeId === "attachment" && content) {
        attachment = {
          filename: content.filename || "unknown",
          mimeType: content.mimeType || "application/octet-stream",
          size: content.content?.length || 0,
          data: content.content ? Buffer.from(content.content).toString("base64") : null,
        };
        content = `[attachment: ${attachment.filename} (${attachment.mimeType}, ${attachment.size} bytes)]`;
      } else if (contentTypeId === "remoteAttachment" && content) {
        attachment = {
          type: "remote",
          url: content.url,
          contentDigest: content.contentDigest,
          filename: content.filename || "unknown",
          mimeType: content.mimeType || "application/octet-stream",
        };
        content = `[remote-attachment: ${attachment.url}]`;
      }

      const entry = {
        id: message.id,
        senderInboxId: message.senderInboxId,
        conversationId: message.conversationId,
        isGroup: false,
        contentType: contentTypeId,
        content,
        attachment,
        sentAt: message.sentAt?.toISOString() || new Date().toISOString(),
        receivedAt: new Date().toISOString(),
      };

      inbox.push(entry);
      if (inbox.length > MAX_INBOX) inbox.shift();

      // Push to SSE clients
      for (const client of sseClients) {
        try { client.write(`data: ${JSON.stringify(entry)}\n\n`); } catch (_) {}
      }

      console.log(`[XMTP] ${contentTypeId} from ${message.senderInboxId.slice(0, 12)}... [inbox=${inbox.length}]`);
    } catch (err) {
      console.error(`[XMTP] Error processing message:`, err.message);
    }
  }
})();

// ---------------------------------------------------------------------------
// Express HTTP API (localhost only) — set up AFTER stream
// ---------------------------------------------------------------------------
const app = express();
app.use(express.json({ limit: "10mb" }));

// Health check
app.get("/health", (_req, res) => {
  res.json({
    status: xmtpClient ? "connected" : "disconnecting",
    address: walletAddress,
    inboxId: xmtpClient?.inboxId || null,
    env: XMTP_ENV,
    inbox_count: inbox.length,
    uptime: process.uptime(),
  });
});

// Send text message via XMTP
app.post("/send", async (req, res) => {
  try {
    const { to, content } = req.body;
    if (!to || !content) {
      return res.status(400).json({ error: "missing 'to' (wallet address) or 'content'" });
    }
    const targetAddress = to.toLowerCase();
    const dm = await getDm(targetAddress);
    const messageText = typeof content === "string" ? content : JSON.stringify(content);
    const msgId = await dm.sendText(messageText);

    res.json({
      status: "sent",
      messageId: msgId,
      to: targetAddress,
      conversationId: dm.id,
    });
  } catch (err) {
    console.error("[XMTP] Send error:", err.message);
    res.status(500).json({ error: err.message });
  }
});

// Get inbox messages
app.get("/inbox", (req, res) => {
  const since = req.query.since ? new Date(req.query.since) : null;
  const clear = req.query.clear === "1" || req.query.clear === "true";

  let messages = inbox;
  if (since) {
    messages = inbox.filter((m) => new Date(m.receivedAt) > since);
  }

  const result = { messages: [...messages], count: messages.length };

  if (clear) {
    inbox.length = 0;
  }

  res.json(result);
});

// Clear inbox
app.post("/clear-inbox", (_req, res) => {
  const count = inbox.length;
  inbox.length = 0;
  res.json({ cleared: count });
});

// Check if an address can receive XMTP messages
app.get("/can-message", async (req, res) => {
  try {
    const address = req.query.address?.toLowerCase();
    if (!address) {
      return res.status(400).json({ error: "missing 'address' query param" });
    }
    const result = await xmtpClient.canMessage([ethIdentifier(address)]);
    res.json({ address, canMessage: result.get(address) || false });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// Send inline attachment (< 1MB)
app.post("/send-attachment", async (req, res) => {
  try {
    const { to, filename, mimeType, content } = req.body;
    if (!to || !filename || !content) {
      return res.status(400).json({ error: "missing 'to', 'filename', or 'content' (base64)" });
    }
    const targetAddress = to.toLowerCase();
    const dm = await getDm(targetAddress);

    const contentBytes = new Uint8Array(Buffer.from(content, "base64"));
    if (contentBytes.length > 1_000_000) {
      return res.status(413).json({
        error: `Attachment too large (${contentBytes.length} bytes). Use /send-remote-attachment for files > 1MB.`,
      });
    }

    const msgId = await dm.sendAttachment({
      filename,
      mimeType: mimeType || "application/octet-stream",
      content: contentBytes,
    });

    res.json({
      status: "sent",
      messageId: msgId,
      to: targetAddress,
      conversationId: dm.id,
      type: "attachment",
      filename,
      size: contentBytes.length,
    });
  } catch (err) {
    console.error("[XMTP] Send attachment error:", err.message);
    res.status(500).json({ error: err.message });
  }
});

// Send remote attachment (any size, stored externally)
app.post("/send-remote-attachment", async (req, res) => {
  try {
    const { to, url, contentDigest, secret, salt, nonce, scheme } = req.body;
    if (!to || !url || !contentDigest || !secret || !salt || !nonce) {
      return res.status(400).json({
        error: "missing required fields: to, url, contentDigest, secret, salt, nonce",
      });
    }
    const targetAddress = to.toLowerCase();
    const dm = await getDm(targetAddress);

    const msgId = await dm.sendRemoteAttachment({
      url,
      contentDigest,
      secret,
      salt,
      nonce,
      scheme: scheme || "https://",
    });

    res.json({
      status: "sent",
      messageId: msgId,
      to: targetAddress,
      conversationId: dm.id,
      type: "remote_attachment",
      url,
    });
  } catch (err) {
    console.error("[XMTP] Send remote attachment error:", err.message);
    res.status(500).json({ error: err.message });
  }
});

// Broadcast: send same message to multiple recipients
app.post("/broadcast", async (req, res) => {
  try {
    const { to, content } = req.body;
    if (!to || !Array.isArray(to) || !content) {
      return res.status(400).json({ error: "missing 'to' (array of wallets) or 'content'" });
    }
    const results = [];
    for (const addr of to) {
      try {
        const targetAddress = addr.toLowerCase();
        const dm = await getDm(targetAddress);
        const messageText = typeof content === "string" ? content : JSON.stringify(content);
        const msgId = await dm.sendText(messageText);
        results.push({ to: targetAddress, status: "sent", messageId: msgId });
      } catch (err) {
        results.push({ to: addr, status: "failed", error: err.message });
      }
    }
    res.json({ status: "broadcast_complete", results, total: to.length });
  } catch (err) {
    console.error("[XMTP] Broadcast error:", err.message);
    res.status(500).json({ error: err.message });
  }
});

// ---------------------------------------------------------------------------
// Group Chat API
// ---------------------------------------------------------------------------

// Create a group conversation with multiple members
app.post("/create-group", async (req, res) => {
  try {
    const { members, name, description } = req.body;
    if (!members || !Array.isArray(members) || members.length === 0) {
      return res.status(400).json({ error: "missing 'members' (array of wallet addresses)" });
    }

    const identifiers = members.map((addr) => ethIdentifier(addr.toLowerCase()));
    await xmtpClient.conversations.sync();

    const group = await xmtpClient.conversations.createGroupWithIdentifiers(identifiers, {
      name: name || "CoWorker Group",
      description: description || "",
    });
    await group.sync();

    const groupId = group.id;
    groupCache.set(groupId, group);

    console.log(`[XMTP] Created group: ${groupId} (${members.length} members, name: "${name || 'CoWorker Group'}")`);

    res.json({
      status: "created",
      groupId,
      name: name || "CoWorker Group",
      members: members.map((a) => a.toLowerCase()),
      memberCount: members.length + 1,
    });
  } catch (err) {
    console.error("[XMTP] Create group error:", err.message);
    res.status(500).json({ error: err.message });
  }
});

// Send a message to a group
app.post("/group-send", async (req, res) => {
  try {
    const { groupId, content } = req.body;
    if (!groupId || !content) {
      return res.status(400).json({ error: "missing 'groupId' or 'content'" });
    }

    const group = await getGroup(groupId);
    const messageText = typeof content === "string" ? content : JSON.stringify(content);
    const msgId = await group.sendText(messageText);

    res.json({
      status: "sent",
      messageId: msgId,
      groupId,
    });
  } catch (err) {
    console.error("[XMTP] Group send error:", err.message);
    res.status(500).json({ error: err.message });
  }
});

// Add a member to an existing group
app.post("/group-add-member", async (req, res) => {
  try {
    const { groupId, member } = req.body;
    if (!groupId || !member) {
      return res.status(400).json({ error: "missing 'groupId' or 'member' (wallet address)" });
    }

    const group = await getGroup(groupId);
    await group.addMembersByIdentifiers([ethIdentifier(member.toLowerCase())]);
    await group.sync();

    console.log(`[XMTP] Added ${member} to group ${groupId}`);
    res.json({ status: "added", groupId, member: member.toLowerCase() });
  } catch (err) {
    console.error("[XMTP] Group add member error:", err.message);
    res.status(500).json({ error: err.message });
  }
});

// Remove a member from a group
app.post("/group-remove-member", async (req, res) => {
  try {
    const { groupId, member } = req.body;
    if (!groupId || !member) {
      return res.status(400).json({ error: "missing 'groupId' or 'member'" });
    }

    const group = await getGroup(groupId);
    await group.removeMembersByIdentifiers([ethIdentifier(member.toLowerCase())]);
    await group.sync();

    console.log(`[XMTP] Removed ${member} from group ${groupId}`);
    res.json({ status: "removed", groupId, member: member.toLowerCase() });
  } catch (err) {
    console.error("[XMTP] Group remove member error:", err.message);
    res.status(500).json({ error: err.message });
  }
});

// List all groups
app.get("/groups", async (_req, res) => {
  try {
    await xmtpClient.conversations.sync();
    const groupList = xmtpClient.conversations.listGroups();
    const groups = groupList.map((g) => ({
      groupId: g.id,
      name: g.name || "Unnamed",
      createdAt: g.createdAt?.toISOString() || null,
    }));

    res.json({ groups, count: groups.length });
  } catch (err) {
    console.error("[XMTP] List groups error:", err.message);
    res.status(500).json({ error: err.message });
  }
});

// SSE stream: real-time push of incoming messages
app.get("/stream", (req, res) => {
  res.writeHead(200, {
    "Content-Type": "text/event-stream",
    "Cache-Control": "no-cache",
    Connection: "keep-alive",
  });
  res.write(`data: ${JSON.stringify({ event: "connected", address: walletAddress })}\n\n`);
  sseClients.add(res);
  req.on("close", () => sseClients.delete(res));
});

// ---------------------------------------------------------------------------
// Start HTTP server
// ---------------------------------------------------------------------------
app.listen(BRIDGE_PORT, "127.0.0.1", () => {
  console.log(`[Bridge] HTTP API listening on http://127.0.0.1:${BRIDGE_PORT}`);
  console.log(`[Bridge] Ready — send messages via POST /send, read via GET /inbox`);
});

/** The only place that talks to the desk server. */

import type { DeskReport, ReviewResponse, ServerMessage } from "./types";

export class ApiError extends Error {}

/** POST a control. Rejects with the server's own explanation when it refuses. */
export async function postControl(path: string, body: object = {}): Promise<void> {
  let response: Response;
  try {
    response = await fetch(`/api${path}`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch {
    throw new ApiError("The desk server is not reachable.");
  }
  if (response.ok) return;
  let detail = `The desk refused the request (${response.status}).`;
  try {
    const payload = await response.json();
    if (typeof payload.detail === "string") detail = payload.detail;
  } catch {
    // Keep the generic message when the body is not JSON.
  }
  throw new ApiError(detail);
}

/** GET a JSON resource. Rejects with an ApiError when the server is unreachable or refuses. */
async function getJson<Payload>(path: string): Promise<Payload> {
  let response: Response;
  try {
    response = await fetch(`/api${path}`);
  } catch {
    throw new ApiError("The desk server is not reachable.");
  }
  if (!response.ok) throw new ApiError(`The desk refused the request (${response.status}).`);
  try {
    return (await response.json()) as Payload;
  } catch {
    throw new ApiError("The desk sent an answer that could not be read.");
  }
}

/** The current run's closed trades with their results and breakdowns. */
export function fetchReview(): Promise<ReviewResponse> {
  return getJson<ReviewResponse>("/review");
}

/** The six-agent report saved when a closed trade was approved. */
export function fetchTradeDecision(tradeNumber: number): Promise<DeskReport> {
  return getJson<DeskReport>(`/review/trades/${tradeNumber}/decision`);
}

export function eventsUrl(location: Pick<Location, "protocol" | "host">): string {
  return `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/ws/events`;
}

export const RECONNECT_DELAY_MS = 1500;

export interface EventStreamOptions {
  url: string;
  onMessage: (message: ServerMessage) => void;
  onConnectionChange: (connection: "connecting" | "open" | "closed") => void;
  createSocket?: (url: string) => WebSocket;
  reconnectDelayMs?: number;
}

/** Opens the event stream and keeps it open, reconnecting after a drop. Each new
 *  connection starts with a full snapshot, so nothing is lost across a reconnect.
 *  Returns a function that closes the stream for good. */
export function connectEvents(options: EventStreamOptions): () => void {
  const createSocket = options.createSocket ?? ((url: string) => new WebSocket(url));
  let socket: WebSocket | null = null;
  let retryTimer: ReturnType<typeof setTimeout> | null = null;
  let closedForGood = false;

  const open = () => {
    options.onConnectionChange("connecting");
    socket = createSocket(options.url);
    socket.onopen = () => options.onConnectionChange("open");
    socket.onmessage = (event) => {
      try {
        options.onMessage(JSON.parse(String(event.data)) as ServerMessage);
      } catch {
        // A frame that is not JSON is ignored; the next snapshot corrects the view.
      }
    };
    socket.onclose = () => {
      if (closedForGood) return;
      options.onConnectionChange("closed");
      retryTimer = setTimeout(open, options.reconnectDelayMs ?? RECONNECT_DELAY_MS);
    };
  };
  open();

  return () => {
    closedForGood = true;
    if (retryTimer !== null) clearTimeout(retryTimer);
    socket?.close();
  };
}

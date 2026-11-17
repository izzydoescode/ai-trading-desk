import { ApiError, connectEvents, eventsUrl, postControl, RECONNECT_DELAY_MS } from "./api";
import { cloneSnapshot } from "./test/fixtures";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("postControl", () => {
  it("posts JSON to the API path", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    await postControl("/replay/speed", { bars_per_second: 8 });
    expect(fetchMock).toHaveBeenCalledWith("/api/replay/speed", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: '{"bars_per_second":8}',
    });
    await postControl("/replay/pause");
    expect(fetchMock.mock.calls[1]![1].body).toBe("{}");
  });

  it("rejects with the server's explanation", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response('{"detail":"A replay is already running"}', { status: 409 }),
      ),
    );
    await expect(postControl("/replay/start")).rejects.toThrow(
      new ApiError("A replay is already running"),
    );
  });

  it("falls back to a generic message for unexpected bodies", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(new Response("<html>bad gateway</html>", { status: 502 }))
      .mockResolvedValueOnce(new Response('{"detail":[{"msg":"field required"}]}', { status: 422 }));
    vi.stubGlobal("fetch", fetchMock);
    await expect(postControl("/x")).rejects.toThrow("The desk refused the request (502).");
    await expect(postControl("/x")).rejects.toThrow("The desk refused the request (422).");
  });

  it("says so when the server cannot be reached", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));
    await expect(postControl("/replay/start")).rejects.toThrow("The desk server is not reachable.");
  });
});

describe("eventsUrl", () => {
  it("matches the page's scheme and host", () => {
    expect(eventsUrl({ protocol: "http:", host: "127.0.0.1:5173" })).toBe(
      "ws://127.0.0.1:5173/ws/events",
    );
    expect(eventsUrl({ protocol: "https:", host: "desk.example" })).toBe(
      "wss://desk.example/ws/events",
    );
  });
});

class FakeSocket {
  static created: FakeSocket[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  closed = false;
  constructor(public url: string) {
    FakeSocket.created.push(this);
  }
  close() {
    this.closed = true;
    this.onclose?.();
  }
}

describe("connectEvents", () => {
  beforeEach(() => {
    FakeSocket.created = [];
    vi.useFakeTimers();
  });

  const connect = () => {
    const onMessage = vi.fn();
    const onConnectionChange = vi.fn();
    const close = connectEvents({
      url: "ws://desk/ws/events",
      onMessage,
      onConnectionChange,
      createSocket: (url) => new FakeSocket(url) as unknown as WebSocket,
    });
    return { onMessage, onConnectionChange, close };
  };

  it("reports connection state and delivers parsed messages", () => {
    const { onMessage, onConnectionChange } = connect();
    const socket = FakeSocket.created[0]!;
    expect(socket.url).toBe("ws://desk/ws/events");
    expect(onConnectionChange).toHaveBeenLastCalledWith("connecting");
    socket.onopen!();
    expect(onConnectionChange).toHaveBeenLastCalledWith("open");
    const snapshot = cloneSnapshot();
    socket.onmessage!({ data: JSON.stringify(snapshot) });
    expect(onMessage).toHaveBeenCalledWith(snapshot);
  });

  it("ignores a frame that is not JSON", () => {
    const { onMessage } = connect();
    FakeSocket.created[0]!.onmessage!({ data: "not json" });
    expect(onMessage).not.toHaveBeenCalled();
  });

  it("reconnects after a drop", () => {
    const { onConnectionChange } = connect();
    FakeSocket.created[0]!.onclose!();
    expect(onConnectionChange).toHaveBeenLastCalledWith("closed");
    expect(FakeSocket.created).toHaveLength(1);
    vi.advanceTimersByTime(RECONNECT_DELAY_MS);
    expect(FakeSocket.created).toHaveLength(2);
    expect(onConnectionChange).toHaveBeenLastCalledWith("connecting");
  });

  it("stays closed once closed on purpose, even mid-retry", () => {
    const { onConnectionChange, close } = connect();
    FakeSocket.created[0]!.onclose!();
    close();
    vi.advanceTimersByTime(RECONNECT_DELAY_MS * 3);
    expect(FakeSocket.created).toHaveLength(1);

    const second = connect();
    const socket = FakeSocket.created[1]!;
    second.close();
    expect(socket.closed).toBe(true);
    expect(second.onConnectionChange).not.toHaveBeenCalledWith("closed");
    vi.advanceTimersByTime(RECONNECT_DELAY_MS * 3);
    expect(FakeSocket.created).toHaveLength(2);
    expect(onConnectionChange).toHaveBeenCalledTimes(2);
  });

  it("uses the browser WebSocket by default", () => {
    vi.stubGlobal("WebSocket", FakeSocket);
    const close = connectEvents({ url: "ws://x/ws/events", onMessage: vi.fn(), onConnectionChange: vi.fn() });
    expect(FakeSocket.created).toHaveLength(1);
    close();
  });
});

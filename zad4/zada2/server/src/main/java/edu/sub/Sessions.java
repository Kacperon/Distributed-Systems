package edu.sub;

import edu.sub.proto.CanUpdate;
import edu.sub.proto.MessageCategory;
import edu.sub.proto.SubscribeResponse;
import io.grpc.Status;
import io.grpc.stub.StreamObserver;

import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Deque;
import java.util.HashMap;
import java.util.HashSet;
import java.util.Iterator;
import java.util.List;
import java.util.Map;
import java.util.Set;

public class Sessions {

    static class State {
        Set<MessageCategory> cats = new HashSet<>();
        Set<String> names = new HashSet<>();
        boolean active = false;
        Deque<CanUpdate> buffer = new ArrayDeque<>();
        int dropped = 0;
        StreamObserver<SubscribeResponse> tx = null;
        long disconnectAt = 0;
    }

    public static class AttachResult {
        public final boolean resumed;
        public final int dropped;
        public final List<CanUpdate> bufferSnapshot;
        AttachResult(boolean r, int d, List<CanUpdate> b) {
            resumed = r; dropped = d; bufferSnapshot = b;
        }
    }

    private final Map<String, State> sessions = new HashMap<>();
    private final Map<String, long[]> stats = new HashMap<>();
    private final int ttlSec;
    private final int bufCap;

    public Sessions(int ttlSec, int bufCap) {
        this.ttlSec = ttlSec;
        this.bufCap = bufCap;
    }

    public synchronized AttachResult attach(String id, StreamObserver<SubscribeResponse> tx) {
        State s = sessions.get(id);
        boolean resumed = s != null;
        int dropped = 0;
        List<CanUpdate> snap = new ArrayList<>();
        if (s == null) {
            s = new State();
            sessions.put(id, s);
        } else {
            if (s.tx != null) {
                try {
                    s.tx.onError(Status.ABORTED
                            .withDescription("session replaced by newer attach")
                            .asRuntimeException());
                } catch (Exception ignored) {}
            }
            dropped = s.dropped;
            s.dropped = 0;
            snap.addAll(s.buffer);
            s.buffer.clear();
        }
        s.tx = tx;
        s.disconnectAt = 0;
        return new AttachResult(resumed, dropped, snap);
    }

    public synchronized void detach(String id, StreamObserver<SubscribeResponse> tx) {
        State s = sessions.get(id);
        if (s == null) return;
        if (s.tx != tx) return;
        s.tx = null;
        s.disconnectAt = System.currentTimeMillis();
    }

    public synchronized void setFilter(String id, List<MessageCategory> cats, List<String> names) {
        State s = sessions.get(id);
        if (s == null) return;
        s.cats = new HashSet<>(cats);
        s.names = new HashSet<>(names);
        s.active = true;
    }

    public synchronized void clearFilter(String id) {
        State s = sessions.get(id);
        if (s == null) return;
        s.active = false;
    }

    public void dispatch(CanUpdate u) {
        List<StreamObserver<SubscribeResponse>> live = new ArrayList<>();
        synchronized (this) {
            for (State s : sessions.values()) {
                if (!s.active) continue;
                if (!s.cats.isEmpty() && !s.cats.contains(u.getCategory())) continue;
                if (!s.names.isEmpty() && !s.names.contains(u.getMessageName())) continue;
                if (s.tx != null) {
                    live.add(s.tx);
                } else {
                    if (s.buffer.size() >= bufCap) {
                        s.buffer.pollFirst();
                        s.dropped++;
                    }
                    s.buffer.addLast(u);
                }
            }
        }
        SubscribeResponse resp = SubscribeResponse.newBuilder().setUpdate(u).build();
        for (StreamObserver<SubscribeResponse> tx : live) {
            try {
                tx.onNext(resp);
            } catch (Exception e) {
                // observer will be detached when grpc reports the failure
            }
        }
    }

    public synchronized int purgeExpired() {
        long now = System.currentTimeMillis();
        int removed = 0;
        Iterator<Map.Entry<String, State>> it = sessions.entrySet().iterator();
        while (it.hasNext()) {
            Map.Entry<String, State> e = it.next();
            State s = e.getValue();
            if (s.tx == null && s.disconnectAt > 0 && now - s.disconnectAt > ttlSec * 1000L) {
                it.remove();
                removed++;
            }
        }
        return removed;
    }

    public synchronized void recordStats(CanUpdate u) {
        long[] s = stats.computeIfAbsent(u.getMessageName(), k -> new long[2]);
        s[0] = u.getTimestampMs();
        s[1]++;
    }

    public synchronized long[] getStats(String name) {
        long[] s = stats.get(name);
        if (s == null) return null;
        return new long[] { s[0], s[1] };
    }

    public synchronized int sessionCount() {
        return sessions.size();
    }
}

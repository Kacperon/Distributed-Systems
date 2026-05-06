package edu.sub;

import edu.sub.proto.Cache;
import edu.sub.proto.CanServiceGrpc;
import edu.sub.proto.CanUpdate;
import edu.sub.proto.ListMessagesRequest;
import edu.sub.proto.ListMessagesResponse;
import edu.sub.proto.MessageStats;
import edu.sub.proto.SessionInfo;
import edu.sub.proto.StatsRequest;
import edu.sub.proto.SubscribeRequest;
import edu.sub.proto.SubscribeResponse;
import io.grpc.Status;
import io.grpc.stub.StreamObserver;

public class CanServiceImpl extends CanServiceGrpc.CanServiceImplBase {

    private final Sessions sessions;

    public CanServiceImpl(Sessions sessions) {
        this.sessions = sessions;
    }

    @Override
    public StreamObserver<SubscribeRequest> subscribe(StreamObserver<SubscribeResponse> respObs) {
        String id = SessionIdInterceptor.SESSION_ID.get();
        if (id == null || id.isEmpty()) {
            respObs.onError(Status.INVALID_ARGUMENT
                    .withDescription("session-id header is required")
                    .asRuntimeException());
            return new StreamObserver<>() {
                public void onNext(SubscribeRequest v) {}
                public void onError(Throwable t) {}
                public void onCompleted() {}
            };
        }

        Sessions.AttachResult r = sessions.attach(id, respObs);
        String tag = shortId(id);
        System.out.println("[subscribe] attach id=" + tag
                + " resumed=" + r.resumed + " buffered=" + r.bufferSnapshot.size()
                + " dropped=" + r.dropped);

        if (r.resumed) {
            respObs.onNext(SubscribeResponse.newBuilder()
                    .setSessionInfo(SessionInfo.newBuilder()
                            .setResumed(true)
                            .setDroppedCount(r.dropped)
                            .build())
                    .build());
            if (!r.bufferSnapshot.isEmpty()) {
                respObs.onNext(SubscribeResponse.newBuilder()
                        .setSnapshot(Cache.newBuilder()
                                .addAllEntries(r.bufferSnapshot)
                                .build())
                        .build());
            }
        }

        final String sid = id;
        return new StreamObserver<>() {
            @Override
            public void onNext(SubscribeRequest req) {
                if (req.getUnsubscribe()) {
                    sessions.clearFilter(sid);
                    System.out.println("[subscribe] unsubscribe id=" + tag);
                } else {
                    sessions.setFilter(sid, req.getCategoriesList(), req.getMessageNamesList());
                    System.out.println("[subscribe] set_filter id=" + tag
                            + " cats=" + req.getCategoriesList()
                            + " names=" + req.getMessageNamesList());
                }
            }

            @Override
            public void onError(Throwable t) {
                System.out.println("[subscribe] error id=" + tag + " : " + t.getMessage());
                sessions.detach(sid, respObs);
            }

            @Override
            public void onCompleted() {
                System.out.println("[subscribe] completed id=" + tag);
                sessions.detach(sid, respObs);
                try { respObs.onCompleted(); } catch (Exception ignored) {}
            }
        };
    }

    private static String shortId(String id) {
        return id.length() > 8 ? id.substring(0, 8) : id;
    }

    @Override
    public void listMessages(ListMessagesRequest req, StreamObserver<ListMessagesResponse> obs) {
        obs.onNext(ListMessagesResponse.newBuilder()
                .addAllMessages(Catalog.listAll())
                .build());
        obs.onCompleted();
    }

    @Override
    public void getStats(StatsRequest req, StreamObserver<MessageStats> obs) {
        String name = req.getMessageName();
        if (name == null || name.isEmpty()) {
            obs.onError(Status.INVALID_ARGUMENT
                    .withDescription("message_name is required")
                    .asRuntimeException());
            return;
        }
        if (!Catalog.exists(name)) {
            obs.onError(Status.NOT_FOUND
                    .withDescription("unknown message: " + name)
                    .asRuntimeException());
            return;
        }
        long[] s = sessions.getStats(name);
        MessageStats.Builder b = MessageStats.newBuilder();
        if (s != null) {
            b.setLastSeenMs(s[0]).setTotalCount(s[1]);
        }
        obs.onNext(b.build());
        obs.onCompleted();
    }
}

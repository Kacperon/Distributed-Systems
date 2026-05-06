package catalog

import io.grpc.ServerBuilder
import io.grpc.protobuf.services.ProtoReflectionService

import scala.concurrent.ExecutionContext

object CatalogServer {
  def main(args: Array[String]): Unit = {
    val port = if (args.nonEmpty) args(0).toInt else 50061
    implicit val ec: ExecutionContext = ExecutionContext.global

    val service = new CatalogImpl()

    val server = ServerBuilder
      .forPort(port)
      .addService(CatalogGrpc.bindService(service, ec))
      .addService(ProtoReflectionService.newInstance())
      .build()

    server.start()
    println(s"[catalog] server listening on $port (reflection enabled)")

    sys.addShutdownHook {
      println("[catalog] shutting down")
      server.shutdown()
    }

    server.awaitTermination()
  }
}

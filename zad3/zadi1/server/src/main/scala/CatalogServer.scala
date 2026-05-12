package library

import com.zeroc.Ice.{Util, Communicator}

object CatalogServer {
  def main(args: Array[String]): Unit = {
    val port = if (args.nonEmpty) args(0).toInt else 10000
    val host = "0.0.0.0"

    val communicator: Communicator = Util.initialize(args)
    try {
      val endpoint = s"tcp -h $host -p $port"
      val adapter = communicator.createObjectAdapterWithEndpoints("CatalogAdapter", endpoint)
      val identity = Util.stringToIdentity("catalog")
      adapter.add(new CatalogImpl(), identity)
      adapter.activate()

      println(s"[catalog] server listening on $endpoint identity='catalog'")
      println(s"[catalog] proxy:  catalog:tcp -h <host> -p $port")

      Runtime.getRuntime.addShutdownHook(new Thread(() => {
        println("[catalog] shutting down")
        communicator.shutdown()
      }))

      communicator.waitForShutdown()
    } finally {
      communicator.destroy()
    }
  }
}

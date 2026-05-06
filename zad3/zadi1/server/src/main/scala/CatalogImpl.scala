package catalog

import com.google.protobuf.empty.Empty
import io.grpc.Status
import io.grpc.stub.StreamObserver

import java.util.concurrent.atomic.AtomicInteger
import scala.collection.concurrent.TrieMap
import scala.concurrent.{ExecutionContext, Future}

class CatalogImpl(implicit ec: ExecutionContext) extends CatalogGrpc.Catalog {
  private val store = TrieMap.empty[Int, Book]
  private val nextId = new AtomicInteger(1)

  override def addBook(request: AddBookRequest): Future[BookId] = Future {
    if (request.title.trim.isEmpty || request.author.trim.isEmpty) {
      throw Status.INVALID_ARGUMENT
        .withDescription("title and author must not be empty")
        .asRuntimeException()
    }
    if (request.year < 0) {
      throw Status.INVALID_ARGUMENT
        .withDescription("year must be non-negative")
        .asRuntimeException()
    }
    val duplicate = store.values.exists(b =>
      b.title.equalsIgnoreCase(request.title) && b.author.equalsIgnoreCase(request.author)
    )
    if (duplicate) {
      throw Status.ALREADY_EXISTS
        .withDescription(s"book '${request.title}' by '${request.author}' already exists")
        .asRuntimeException()
    }
    val id = nextId.getAndIncrement()
    val book = Book(
      id = id,
      title = request.title,
      author = request.author,
      year = request.year,
      tags = request.tags
    )
    store.put(id, book)
    println(s"[catalog] AddBook id=$id title='${request.title}' author='${request.author}'")
    BookId(id = id)
  }

  override def findByAuthor(request: AuthorQuery, responseObserver: StreamObserver[Book]): Unit = {
    println(s"[catalog] FindByAuthor author='${request.author}' limit=${request.limit}")
    if (request.author.trim.isEmpty) {
      responseObserver.onError(
        Status.INVALID_ARGUMENT
          .withDescription("author must not be empty")
          .asRuntimeException()
      )
    } else {
      val needle = request.author.toLowerCase
      val matches = store.values.toList
        .filter(_.author.toLowerCase.contains(needle))
        .sortBy(_.id)
      val limited = if (request.limit > 0) matches.take(request.limit) else matches
      limited.foreach { b =>
        println(s"[catalog]   -> id=${b.id} '${b.title}'")
        responseObserver.onNext(b)
      }
      responseObserver.onCompleted()
    }
  }

  override def summary(request: Empty): Future[CatalogStats] = Future {
    println("[catalog] Summary")
    val all = store.values.toList
    val byAuthor = all.groupBy(_.author).view.mapValues(_.size).toMap
    val recent = all.sortBy(-_.id).take(5)
    CatalogStats(total = all.size, byAuthor = byAuthor, recent = recent)
  }

  override def removeBook(request: BookId): Future[Empty] = Future {
    store.remove(request.id) match {
      case Some(b) =>
        println(s"[catalog] RemoveBook id=${request.id} title='${b.title}'")
        Empty()
      case None =>
        throw Status.NOT_FOUND
          .withDescription(s"book id=${request.id} not found")
          .asRuntimeException()
    }
  }
}

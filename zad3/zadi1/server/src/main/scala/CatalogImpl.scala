package library

import com.zeroc.Ice.Current

import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.atomic.AtomicInteger
import scala.jdk.CollectionConverters._

class CatalogImpl extends Catalog {
  private val store = new ConcurrentHashMap[Integer, Book]()
  private val nextId = new AtomicInteger(0)

  override def addBook(request: AddBookRequest, current: Current): AddBookResult = {
    if (request.title == null || request.title.trim.isEmpty)
      return new AddBookResult(0, "INVALID_ARGUMENT", "title must not be empty")
    if (request.author == null || request.author.trim.isEmpty)
      return new AddBookResult(0, "INVALID_ARGUMENT", "author must not be empty")
    if (request.year < 0)
      return new AddBookResult(0, "INVALID_ARGUMENT", "year must be non-negative")

    val tags = if (request.tags == null) Array.empty[String] else request.tags

    val duplicate = store.values.iterator.asScala.exists { b =>
      b.title.equalsIgnoreCase(request.title) && b.author.equalsIgnoreCase(request.author)
    }
    if (duplicate)
      return new AddBookResult(0, "ALREADY_EXISTS", s"book '${request.title}' by '${request.author}' already exists")

    val id = nextId.incrementAndGet()
    val book = new Book(id, request.title, request.author, request.year, tags)
    store.put(id, book)
    println(s"[catalog] addBook id=$id title='${request.title}' author='${request.author}'")
    new AddBookResult(id, "", "")
  }

  override def findByAuthor(query: AuthorQuery, observer: BookStreamPrx, current: Current): Unit = {
    println(s"[catalog] findByAuthor author='${query.author}' limit=${query.limit}")
    if (observer == null) return
    if (query.author == null || query.author.trim.isEmpty) {
      observer.onError("INVALID_ARGUMENT", "author must not be empty")
      return
    }
    val needle = query.author.toLowerCase
    val matches = store.values.iterator.asScala.toList
      .filter(_.author.toLowerCase.contains(needle))
      .sortBy(_.id)
    val limited = if (query.limit > 0) matches.take(query.limit) else matches
    try {
      limited.foreach { b =>
        println(s"[catalog]   -> id=${b.id} '${b.title}'")
        observer.onNext(b)
      }
      observer.onCompleted()
    } catch {
      case e: com.zeroc.Ice.LocalException =>
        println(s"[catalog]   stream observer failed: ${e.getMessage}")
    }
  }

  override def summary(current: Current): CatalogStats = {
    println("[catalog] summary")
    val all = store.values.iterator.asScala.toList
    val byAuthor: java.util.Map[String, Integer] =
      all.groupBy(_.author).map { case (k, v) => (k, Integer.valueOf(v.size)) }.asJava
    val recent = all.sortBy(-_.id).take(5).toArray
    new CatalogStats(all.size, byAuthor, recent)
  }

  override def removeBook(id: Int, current: Current): RemoveBookResult = {
    val removed = store.remove(id)
    if (removed == null) {
      new RemoveBookResult(false, "NOT_FOUND", s"book id=$id not found")
    } else {
      println(s"[catalog] removeBook id=$id title='${removed.title}'")
      new RemoveBookResult(true, "", "")
    }
  }
}

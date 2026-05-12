import scala.sys.process.Process

ThisBuild / scalaVersion := "2.13.16"
ThisBuild / version := "0.1.0"

lazy val root = (project in file("."))
  .settings(
    name := "zadi1-server",
    libraryDependencies ++= Seq(
      "com.zeroc" % "ice" % "3.7.10"
    ),
    Compile / sourceGenerators += Def.task {
      val sliceDir = (Compile / sourceDirectory).value / "slice"
      val outDir = (Compile / sourceManaged).value / "slice-java"
      val cache = streams.value.cacheDirectory / "slice"
      val sliceFiles = (sliceDir ** "*.ice").get.toSet
      val cached = FileFunction.cached(cache, FilesInfo.lastModified, FilesInfo.exists) { _ =>
        IO.delete(outDir)
        IO.createDirectory(outDir)
        sliceFiles.foreach { f =>
          val rc = Process(Seq("slice2java", "--output-dir", outDir.getAbsolutePath, f.getAbsolutePath)).!
          if (rc != 0) sys.error(s"slice2java failed for ${f.getName} (rc=$rc)")
        }
        (outDir ** "*.java").get.toSet
      }
      cached(sliceFiles).toSeq
    }.taskValue,
    run / fork := true,
    Compile / mainClass := Some("library.CatalogServer")
  )

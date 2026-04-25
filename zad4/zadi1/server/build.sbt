ThisBuild / scalaVersion := "2.13.16"
ThisBuild / version := "0.1.0"

lazy val root = (project in file("."))
  .settings(
    name := "zadi1-server",
    libraryDependencies ++= Seq(
      "io.grpc" % "grpc-netty-shaded" % scalapb.compiler.Version.grpcJavaVersion,
      "io.grpc" % "grpc-services" % scalapb.compiler.Version.grpcJavaVersion,
      "com.thesamet.scalapb" %% "scalapb-runtime-grpc" % scalapb.compiler.Version.scalapbVersion,
      "com.google.protobuf" % "protobuf-java" % "3.25.5" % "protobuf"
    ),
    Compile / PB.protoSources := Seq((Compile / sourceDirectory).value / "proto"),
    Compile / PB.targets := Seq(
      scalapb.gen(flatPackage = true) -> (Compile / sourceManaged).value / "scalapb"
    ),
    run / fork := true,
    Compile / mainClass := Some("catalog.CatalogServer")
  )

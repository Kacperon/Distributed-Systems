module library
{
    sequence<string> StringSeq;

    struct Book
    {
        int id;
        string title;
        string author;
        int year;
        StringSeq tags;
    };

    sequence<Book> BookSeq;
    dictionary<string, int> AuthorCounts;

    struct AddBookRequest
    {
        string title;
        string author;
        int year;
        StringSeq tags;
    };

    struct AuthorQuery
    {
        string author;
        int limit;
    };

    struct CatalogStats
    {
        int total;
        AuthorCounts byAuthor;
        BookSeq recent;
    };

    struct AddBookResult
    {
        int bookId;
        string errorCode;
        string errorMessage;
    };

    struct RemoveBookResult
    {
        bool ok;
        string errorCode;
        string errorMessage;
    };

    interface BookStream
    {
        void onNext(Book book);
        void onCompleted();
        void onError(string code, string message);
    };

    interface Catalog
    {
        AddBookResult addBook(AddBookRequest request);
        void findByAuthor(AuthorQuery query, BookStream* observer);
        CatalogStats summary();
        RemoveBookResult removeBook(int id);
    };
};

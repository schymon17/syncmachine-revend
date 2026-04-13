namespace Revend.EventDesktop;

static class Program
{
    [STAThread]
    static void Main()
    {
        ApplicationConfiguration.Initialize();
        Application.Run(new Form1("http://localhost:21011"));
    }
}